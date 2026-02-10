"""Streamlit web GUI for the portfolio rebalancer."""

import tempfile
from decimal import Decimal, InvalidOperation
from pathlib import Path

import plotly.graph_objects as go
import streamlit as st
import yaml

from rebalancer.config import load_mapping, load_unified_config
from rebalancer.engine import rebalance
from rebalancer.fx import BankCashAccount, convert_bank_cash_to_positions, fetch_fx_rate
from rebalancer.german_tax import annotate_trades, generate_summary
from rebalancer.models import (
    AccountType,
    AllocationTarget,
    CashConfig,
    GermanTaxConfig,
    OutputConfig,
    PrecisionConfig,
    RebalanceConfig,
    RebalanceResult,
    SortKey,
    TickerMapping,
)
from rebalancer.output import (
    _compute_allocation,
    build_execution_plan,
    filter_actionable_trades,
    sort_trades,
)
from rebalancer.parser import parse_fidelity_csv

# ---------------------------------------------------------------------------
# Page config
# ---------------------------------------------------------------------------
st.set_page_config(
    page_title="Portfolio Rebalancer",
    page_icon="\u2696\ufe0f",
    layout="wide",
)

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------
EXAMPLE_DIR = Path(__file__).parent.parent.parent / "examples"

ASSET_CLASSES = ["cash", "bonds", "reit", "us_equity", "intl_equity"]

SORT_OPTIONS = {
    "Sells first, largest first": [SortKey.SELLS_FIRST, SortKey.LARGEST_TRADE_FIRST],
    "Buys first, largest first": [SortKey.BUYS_FIRST, SortKey.LARGEST_TRADE_FIRST],
    "Largest trade first": [SortKey.LARGEST_TRADE_FIRST],
    "By account": [SortKey.BY_ACCOUNT],
    "By ticker": [SortKey.BY_TICKER],
}

DEFAULT_ACCOUNT_MAPPINGS = {
    "Individual": "taxable",
    "Taxable": "taxable",
    "Brokerage": "taxable",
    "ROTH": "roth_ira",
    "Rollover": "traditional_ira",
    "Traditional": "traditional_ira",
    "401(K)": "401k",
}

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _load_example_text(name: str) -> str:
    p = EXAMPLE_DIR / name
    if p.exists():
        return p.read_text()
    return ""


def _dec(val: Decimal) -> float:
    """Convert Decimal to float for display."""
    return float(val)


def _format_money(val: Decimal, precision: int = 0) -> str:
    if precision == 0:
        return f"${_dec(val):,.0f}"
    return f"${_dec(val):,.{precision}f}"


def _save_temp(content: str, suffix: str) -> Path:
    """Write string content to a temp file and return the path."""
    tmp = tempfile.NamedTemporaryFile(mode="w", suffix=suffix, delete=False)
    tmp.write(content)
    tmp.flush()
    return Path(tmp.name)


# ---------------------------------------------------------------------------
# Session state defaults
# ---------------------------------------------------------------------------
_DEFAULTS = {
    "positions": None,
    "mapping_data": None,
    "result": None,
    "mapping_text": None,
}
for key, default in _DEFAULTS.items():
    if key not in st.session_state:
        st.session_state[key] = default

# ---------------------------------------------------------------------------
# Sidebar
# ---------------------------------------------------------------------------

st.sidebar.title("Configuration")

# --- 1. Positions CSV ---
st.sidebar.header("1. Portfolio Data")
uploaded_csv = st.sidebar.file_uploader(
    "Upload Fidelity positions CSV", type=["csv"], key="csv_upload"
)
use_example_csv = st.sidebar.checkbox(
    "Use example CSV", value=not uploaded_csv, key="use_example"
)

if uploaded_csv:
    csv_path = _save_temp(uploaded_csv.getvalue().decode("utf-8-sig"), ".csv")
elif use_example_csv and (EXAMPLE_DIR / "fidelity_positions.csv").exists():
    csv_path = EXAMPLE_DIR / "fidelity_positions.csv"
else:
    csv_path = None

# --- 2. Target Allocation ---
st.sidebar.header("2. Target Allocation")

alloc_values = {}
for ac in ASSET_CLASSES:
    default_val = {"cash": 10, "bonds": 20, "reit": 5, "us_equity": 45, "intl_equity": 20}.get(ac, 0)
    alloc_values[ac] = st.sidebar.number_input(
        ac,
        min_value=0,
        max_value=100,
        value=default_val,
        step=1,
        key=f"alloc_{ac}",
    )

alloc_sum = sum(alloc_values.values())
if alloc_sum == 100:
    st.sidebar.success(f"Sum: {alloc_sum}%")
elif alloc_sum > 100:
    st.sidebar.error(f"Sum: {alloc_sum}% (must be 100)")
else:
    st.sidebar.warning(f"Sum: {alloc_sum}% (must be 100)")

# --- 3. Ticker Mapping ---
st.sidebar.header("3. Ticker Mapping")
default_mapping = _load_example_text("mapping.yaml")
mapping_text = st.sidebar.text_area(
    "mapping.yaml",
    value=st.session_state.mapping_text or default_mapping,
    height=200,
    key="mapping_input",
)
st.session_state.mapping_text = mapping_text

# --- 4. Rebalance Settings ---
st.sidebar.header("4. Rebalance Settings")
threshold_pct = st.sidebar.number_input(
    "Threshold %",
    min_value=0.0,
    max_value=50.0,
    value=5.0,
    step=0.5,
    key="threshold_pct",
)
min_trade_value = st.sidebar.number_input(
    "Min trade value ($)",
    min_value=0.0,
    value=500.0,
    step=50.0,
    key="min_trade_value",
)

# --- 5. Tax ---
st.sidebar.header("5. Tax")
tax_enabled = st.sidebar.toggle("Tax-aware trading", value=False, key="tax_enabled")

# --- 6. External Cash ---
st.sidebar.header("6. External Cash")
include_in_portfolio = st.sidebar.checkbox(
    "Include external cash in portfolio", value=True, key="include_in_portfolio"
)
bank_usd = st.sidebar.number_input(
    "USD cash ($)",
    min_value=0.0,
    value=0.0,
    step=100.0,
    key="bank_usd",
)
bank_eur = st.sidebar.number_input(
    "EUR cash (\u20ac)",
    min_value=0.0,
    value=0.0,
    step=100.0,
    key="bank_eur",
)
use_live_fx = st.sidebar.checkbox("Fetch live EUR/USD rate", value=True, key="use_live_fx")

_live_rate: Decimal | None = None
if use_live_fx:
    _live_rate = fetch_fx_rate("EUR", "USD")
    if _live_rate is not None:
        st.sidebar.caption(f"Live EUR/USD: {_live_rate}")
    else:
        st.sidebar.caption("Failed to fetch live rate")

fx_default = float(_live_rate) if _live_rate is not None else 1.10
manual_fx = st.sidebar.number_input(
    "EUR/USD rate (fallback)",
    min_value=0.01,
    value=fx_default,
    step=0.01,
    format="%.4f",
    key="manual_fx",
)

# --- 7. Display Settings ---
st.sidebar.header("7. Display Settings")
show_only_actionable = st.sidebar.checkbox(
    "Show only actionable trades", value=True, key="show_only_actionable"
)
sort_label = st.sidebar.selectbox(
    "Sort order",
    options=list(SORT_OPTIONS.keys()),
    index=0,
    key="sort_order",
)
sort_order = SORT_OPTIONS[sort_label]

currency_precision = st.sidebar.radio(
    "Currency precision",
    options=[0, 2],
    format_func=lambda x: "$1,234" if x == 0 else "$1,234.56",
    index=0,
    key="currency_precision",
)

# --- 8. German Tax ---
st.sidebar.header("8. German Tax")
german_tax_enabled = st.sidebar.toggle(
    "Show German tax annotations", value=False, key="german_tax_enabled"
)
german_tax_filing = "single"
if german_tax_enabled:
    german_tax_filing = st.sidebar.selectbox(
        "Filing status",
        options=["single", "married"],
        index=0,
        key="german_tax_filing",
    )

# --- 9. Account Types ---
with st.sidebar.expander("9. Account Types"):
    acct_mapping_text = ""
    for substr, acct_type in DEFAULT_ACCOUNT_MAPPINGS.items():
        acct_mapping_text += f"{substr}: {acct_type}\n"
    acct_yaml = st.text_area(
        "Account substring -> type",
        value=acct_mapping_text,
        height=150,
        key="acct_mapping_input",
    )

# --- 10. Advanced ---
with st.sidebar.expander("10. Advanced"):
    st.caption("Raw unified config YAML (read-only view / apply override)")

    # Build current config from widgets
    _current_unified = {
        "allocation": {ac: alloc_values[ac] for ac in ASSET_CLASSES},
        "rebalance": {
            "threshold_pct": threshold_pct,
            "min_trade_value": min_trade_value,
        },
        "cash": {
            "include_in_portfolio": include_in_portfolio,
            "external_cash_eur": float(bank_eur),
            "external_cash_usd": float(bank_usd),
            "eurusd_fx": float(manual_fx),
        },
        "tax": {"enabled": tax_enabled},
        "output": {
            "show_only_actionable_trades": show_only_actionable,
            "sort_order": [s.value for s in sort_order],
            "precision": {"currency": currency_precision, "pct": 2},
        },
    }

    # Parse account mappings
    try:
        parsed_acct = yaml.safe_load(acct_yaml)
        if isinstance(parsed_acct, dict):
            _current_unified["accounts"] = parsed_acct
    except Exception:
        pass

    unified_yaml_str = yaml.dump(_current_unified, default_flow_style=False, sort_keys=False)
    advanced_yaml = st.text_area(
        "Unified YAML",
        value=unified_yaml_str,
        height=300,
        key="advanced_yaml",
    )
    apply_advanced = st.button("Apply YAML", key="apply_advanced")


# ---------------------------------------------------------------------------
# Build OutputConfig from widgets
# ---------------------------------------------------------------------------
output_config = OutputConfig(
    show_only_actionable_trades=show_only_actionable,
    sort_order=sort_order,
    precision=PrecisionConfig(currency=currency_precision, pct=2),
)

cur_prec = output_config.precision.currency


# ---------------------------------------------------------------------------
# Load data
# ---------------------------------------------------------------------------


def _load_all():
    """Parse all inputs and return (positions, targets, mapping, config, output_config, bank_positions)."""
    if csv_path is None:
        raise ValueError("No positions CSV provided. Upload a file or check 'Use example CSV'.")

    positions = parse_fidelity_csv(csv_path)
    if not positions:
        raise ValueError("No positions found in CSV. Check the file format.")

    # Targets from widget values
    if alloc_sum != 100:
        raise ValueError(f"Target allocations must sum to 100, got {alloc_sum}")
    targets = [
        AllocationTarget(asset_class=ac, target_pct=Decimal(str(alloc_values[ac])))
        for ac in ASSET_CLASSES
        if alloc_values[ac] > 0
    ]

    # Mapping
    map_path = _save_temp(mapping_text, ".yaml")
    mapping = load_mapping(map_path)

    # Account mappings
    account_mappings: dict[str, AccountType] = {}
    try:
        parsed_acct = yaml.safe_load(acct_yaml)
        if isinstance(parsed_acct, dict):
            valid_types = {e.value for e in AccountType}
            for substr, acct_type_str in parsed_acct.items():
                if acct_type_str in valid_types:
                    account_mappings[substr] = AccountType(acct_type_str)
    except Exception:
        pass

    config = RebalanceConfig(
        threshold_pct=Decimal(str(threshold_pct)),
        min_trade_value=Decimal(str(min_trade_value)),
        tlh_enabled=tax_enabled,
        avoid_gains_in_taxable=tax_enabled,
        cash_to_invest=Decimal("0"),
        account_mappings=account_mappings,
    )

    # Build bank cash positions
    eur_usd_rate = Decimal(str(manual_fx))
    bank_accounts: list[BankCashAccount] = []
    if bank_usd > 0:
        bank_accounts.append(
            BankCashAccount(currency="USD", amount=Decimal(str(bank_usd)), account_name="Bank (USD)")
        )
    if bank_eur > 0:
        bank_accounts.append(
            BankCashAccount(currency="EUR", amount=Decimal(str(bank_eur)), account_name="Bank (EUR)")
        )
    bank_positions = convert_bank_cash_to_positions(bank_accounts, eur_usd_rate)

    # Auto-register synthetic cash tickers in the mapping
    for bp in bank_positions:
        if bp.ticker not in mapping:
            mapping[bp.ticker] = TickerMapping(asset_class="cash")

    return positions, targets, mapping, config, output_config, bank_positions


# ---------------------------------------------------------------------------
# Main content
# ---------------------------------------------------------------------------

st.title("Portfolio Rebalancer")

# Tabs
tab_overview, tab_rebalance, tab_trades = st.tabs(
    ["Portfolio Overview", "Rebalance Analysis", "Trade Plan"]
)

# Try to load data
try:
    positions, targets, mapping, config, oc, bank_positions = _load_all()
    all_positions = positions + (bank_positions if include_in_portfolio else [])
    data_ok = True
except Exception as e:
    data_ok = False
    data_error = str(e)

# ---- Tab 1: Portfolio Overview ----
with tab_overview:
    if not data_ok:
        st.error(f"Cannot load data: {data_error}")
    else:
        total_value, value_by_class, pct_by_class = _compute_allocation(
            all_positions, mapping, pct_precision=oc.precision.pct
        )

        # KPI row
        col1, col2, col3 = st.columns(3)
        col1.metric("Total Portfolio Value", _format_money(total_value, cur_prec))
        col2.metric("Accounts", len({p.account_name for p in all_positions}))
        col3.metric("Positions", len(all_positions))

        st.divider()

        # Charts side by side
        chart_col1, chart_col2 = st.columns(2)

        with chart_col1:
            st.subheader("Current Allocation")
            labels = sorted(pct_by_class.keys())
            values = [_dec(pct_by_class[c]) for c in labels]
            fig = go.Figure(
                data=[go.Pie(labels=labels, values=values, hole=0.4)]
            )
            fig.update_layout(margin=dict(t=20, b=20, l=20, r=20), height=350)
            st.plotly_chart(fig, width="stretch")

        with chart_col2:
            st.subheader("Target Allocation")
            tgt_map = {}
            for t in targets:
                tgt_map[t.asset_class] = _dec(t.target_pct)
            tgt_labels = sorted(tgt_map.keys())
            tgt_values = [tgt_map[c] for c in tgt_labels]
            fig2 = go.Figure(
                data=[go.Pie(labels=tgt_labels, values=tgt_values, hole=0.4)]
            )
            fig2.update_layout(margin=dict(t=20, b=20, l=20, r=20), height=350)
            st.plotly_chart(fig2, width="stretch")

        # Positions table
        st.subheader("Positions")
        pos_rows = []
        for p in all_positions:
            ticker_info = mapping.get(p.ticker)
            asset_class = ticker_info.asset_class if ticker_info else "unmapped"
            gain_loss = None
            if p.cost_basis_total is not None:
                gain_loss = p.market_value - p.cost_basis_total
            pos_rows.append(
                {
                    "Account": p.account_name,
                    "Type": p.account_type.value,
                    "Ticker": p.ticker,
                    "Description": p.description,
                    "Shares": f"{_dec(p.quantity):,.3f}" if p.quantity else "-",
                    "Price": _format_money(p.price, cur_prec) if p.price else "-",
                    "Value": _format_money(p.market_value, cur_prec),
                    "Cost Basis": _format_money(p.cost_basis_total, cur_prec) if p.cost_basis_total is not None else "-",
                    "Gain/Loss": _format_money(gain_loss, cur_prec) if gain_loss is not None else "-",
                    "Asset Class": asset_class,
                }
            )
        st.dataframe(pos_rows, width="stretch", hide_index=True)

        # Bank Cash Holdings summary
        if bank_positions and include_in_portfolio:
            st.subheader("Bank Cash Holdings")
            eur_usd_rate = Decimal(str(manual_fx))
            cash_rows = []
            for bp in bank_positions:
                if bp.ticker == "CASH-USD":
                    cash_rows.append({
                        "Currency": "USD",
                        "Original Amount": f"${_dec(bp.quantity):,.2f}",
                        "USD Value": _format_money(bp.market_value, cur_prec),
                    })
                else:
                    cash_rows.append({
                        "Currency": "EUR",
                        "Original Amount": f"\u20ac{_dec(bp.quantity):,.2f}",
                        "USD Value": _format_money(bp.market_value, cur_prec),
                    })
            st.dataframe(cash_rows, width="stretch", hide_index=True)


# ---- Tab 2: Rebalance Analysis ----
with tab_rebalance:
    if not data_ok:
        st.error(f"Cannot load data: {data_error}")
    else:
        result = rebalance(all_positions, targets, mapping, config)
        st.session_state.result = result

        st.subheader("Allocation vs Target")

        all_classes = sorted(
            set(result.current_allocation.keys())
            | set(result.target_allocation.keys())
        )

        # Drift bar chart
        drift_labels = all_classes
        drift_values = [_dec(result.drift.get(c, Decimal("0"))) for c in drift_labels]
        colors = ["#ef4444" if v < 0 else "#22c55e" for v in drift_values]

        fig3 = go.Figure(
            data=[
                go.Bar(
                    x=drift_labels,
                    y=drift_values,
                    marker_color=colors,
                    text=[f"{v:+.2f}%" for v in drift_values],
                    textposition="outside",
                )
            ]
        )
        fig3.update_layout(
            title="Drift from Target (%)",
            yaxis_title="Drift %",
            xaxis_title="Asset Class",
            margin=dict(t=40, b=40),
            height=350,
        )
        fig3.add_hline(y=_dec(config.threshold_pct), line_dash="dash",
                       line_color="orange",
                       annotation_text=f"Threshold ({config.threshold_pct}%)")
        fig3.add_hline(y=-_dec(config.threshold_pct), line_dash="dash",
                       line_color="orange")
        st.plotly_chart(fig3, width="stretch")

        # Comparison table
        alloc_rows = []
        for cls in all_classes:
            current = result.current_allocation.get(cls, Decimal("0"))
            target = result.target_allocation.get(cls, Decimal("0"))
            drift = result.drift.get(cls, Decimal("0"))
            alloc_rows.append(
                {
                    "Asset Class": cls,
                    "Current %": f"{_dec(current):.2f}",
                    "Target %": f"{_dec(target):.2f}",
                    "Drift %": f"{_dec(drift):+.2f}",
                }
            )
        st.dataframe(alloc_rows, width="stretch", hide_index=True)

        # Stacked bar: current vs target
        fig4 = go.Figure()
        fig4.add_trace(
            go.Bar(
                name="Current",
                x=all_classes,
                y=[_dec(result.current_allocation.get(c, Decimal("0"))) for c in all_classes],
            )
        )
        fig4.add_trace(
            go.Bar(
                name="Target",
                x=all_classes,
                y=[_dec(result.target_allocation.get(c, Decimal("0"))) for c in all_classes],
            )
        )
        fig4.update_layout(
            barmode="group",
            title="Current vs Target Allocation (%)",
            yaxis_title="%",
            height=350,
            margin=dict(t=40, b=40),
        )
        st.plotly_chart(fig4, width="stretch")

        # Tax impact (only show when tax-aware trading is enabled)
        if tax_enabled:
            ti = result.tax_impact
            if ti.taxable_trades_count > 0:
                st.subheader("Estimated Tax Impact (Taxable Accounts)")
                ti_col1, ti_col2, ti_col3 = st.columns(3)
                ti_col1.metric("Estimated Gains", _format_money(ti.estimated_total_gains, cur_prec))
                ti_col2.metric("Estimated Losses", _format_money(ti.estimated_total_losses, cur_prec))
                net_delta = "positive" if ti.estimated_net > 0 else "negative" if ti.estimated_net < 0 else None
                ti_col3.metric(
                    "Net",
                    _format_money(ti.estimated_net, cur_prec),
                    delta=f"{_format_money(ti.estimated_net, cur_prec)} taxable" if ti.estimated_net != 0 else None,
                    delta_color="inverse",
                )

        # Warnings
        if result.warnings:
            st.subheader("Warnings")
            for w in result.warnings:
                st.warning(w)


# ---- Tab 3: Trade Plan ----
with tab_trades:
    if not data_ok:
        st.error(f"Cannot load data: {data_error}")
    else:
        result = st.session_state.result
        if result is None:
            st.info("Run the Rebalance Analysis tab first.")
        elif not result.trades:
            st.success("Portfolio is within target thresholds. No trades needed.")
        else:
            # Filter first, then build execution plan from filtered trades
            display_trades = filter_actionable_trades(
                result.trades, config.min_trade_value, show_only_actionable
            )
            hidden_count = len(result.trades) - len(display_trades)
            steps = build_execution_plan(display_trades)
            sell_steps = [s for s in steps if s.phase == "SELL"]
            buy_steps = [s for s in steps if s.phase == "BUY"]
            total_sell = sum(s.trade.estimated_value for s in sell_steps)
            total_buy = sum(s.trade.estimated_value for s in buy_steps)
            sell_accounts = sorted({s.trade.account_name for s in sell_steps})
            buy_accounts = sorted({s.trade.account_name for s in buy_steps})

            # --- Header ---
            st.subheader(f"Execution Plan ({len(steps)} steps)")
            if hidden_count > 0:
                st.caption(f"{hidden_count} small trades hidden (below {_format_money(Decimal(str(min_trade_value)), cur_prec)} threshold)")

            # --- Summary metrics ---
            m1, m2, m3 = st.columns(3)
            m1.metric("Sell first", f"{len(sell_steps)} trades", f"frees {_format_money(total_sell, cur_prec)}")
            m2.metric("Then buy", f"{len(buy_steps)} trades", f"costs {_format_money(total_buy, cur_prec)}")
            m3.metric("Total steps", len(steps))

            # --- How-to ---
            st.info(
                "**How to execute:** Work through each step in order. "
                "Complete all **sells** first to free up cash, then execute the **buys**. "
                "Steps are grouped by account so you can log into one account, complete its trades, then move on."
            )

            # --- Sell phase ---
            if sell_steps:
                st.divider()
                st.markdown(f"### Phase 1: Sells ({len(sell_steps)} trades across {len(sell_accounts)} accounts)")
                st.caption("Sell overweight positions to free up cash for rebalancing.")

                current_account = None
                for s in sell_steps:
                    t = s.trade
                    if t.account_name != current_account:
                        current_account = t.account_name
                        acct_sells = [x for x in sell_steps if x.trade.account_name == current_account]
                        acct_total = sum(x.trade.estimated_value for x in acct_sells)
                        st.markdown(
                            f"**{current_account}** ({t.account_type.value}) "
                            f"--- {len(acct_sells)} trade(s), {_format_money(acct_total, cur_prec)} total"
                        )

                    col_step, col_detail = st.columns([1, 11])
                    with col_step:
                        st.markdown(f"#### {s.step_num}")
                    with col_detail:
                        gain_loss_str = ""
                        if t.estimated_gain_loss is not None:
                            gl = t.estimated_gain_loss
                            if gl > 0:
                                gain_loss_str = f" | Est. gain: {_format_money(gl, cur_prec)}"
                            elif gl < 0:
                                gain_loss_str = f" | Est. loss: {_format_money(gl, cur_prec)}"
                        st.markdown(
                            f"SELL **{t.ticker}** --- "
                            f"**{_dec(t.shares):.3f}** shares --- "
                            f"**{_format_money(t.estimated_value, cur_prec)}**{gain_loss_str}"
                        )
                        st.caption(f"{t.reasoning} | Cash after this step: {_format_money(s.cash_after, cur_prec)}")
                        for w in t.warnings:
                            st.warning(w)

            # --- Buy phase ---
            if buy_steps:
                st.divider()
                st.markdown(f"### Phase 2: Buys ({len(buy_steps)} trades across {len(buy_accounts)} accounts)")
                st.caption("Use the freed cash to buy into underweight positions.")

                current_account = None
                for s in buy_steps:
                    t = s.trade
                    if t.account_name != current_account:
                        current_account = t.account_name
                        acct_buys = [x for x in buy_steps if x.trade.account_name == current_account]
                        acct_total = sum(x.trade.estimated_value for x in acct_buys)
                        st.markdown(
                            f"**{current_account}** ({t.account_type.value}) "
                            f"--- {len(acct_buys)} trade(s), {_format_money(acct_total, cur_prec)} total"
                        )

                    col_step, col_detail = st.columns([1, 11])
                    with col_step:
                        st.markdown(f"#### {s.step_num}")
                    with col_detail:
                        st.markdown(
                            f"BUY **{t.ticker}** --- "
                            f"**{_dec(t.shares):.3f}** shares --- "
                            f"**{_format_money(t.estimated_value, cur_prec)}**"
                        )
                        st.caption(f"{t.reasoning} | Cash after this step: {_format_money(s.cash_after, cur_prec)}")

            # --- Trade breakdown chart ---
            st.divider()
            st.subheader("Trade Values by Ticker")
            tickers = sorted({t.ticker for t in display_trades})
            sell_vals = []
            buy_vals = []
            sells_list = [t for t in display_trades if t.action == "SELL"]
            buys_list = [t for t in display_trades if t.action == "BUY"]
            for ticker in tickers:
                sv = sum(_dec(t.estimated_value) for t in sells_list if t.ticker == ticker)
                bv = sum(_dec(t.estimated_value) for t in buys_list if t.ticker == ticker)
                sell_vals.append(-sv if sv else 0)
                buy_vals.append(bv if bv else 0)

            fig5 = go.Figure()
            fig5.add_trace(
                go.Bar(name="Sell", x=tickers, y=sell_vals, marker_color="#ef4444")
            )
            fig5.add_trace(
                go.Bar(name="Buy", x=tickers, y=buy_vals, marker_color="#22c55e")
            )
            fig5.update_layout(
                barmode="relative",
                title="Trade Plan ($)",
                yaxis_title="$ Value",
                height=350,
                margin=dict(t=40, b=40),
            )
            st.plotly_chart(fig5, width="stretch")

            # Warnings
            if result.warnings:
                st.subheader("Warnings")
                for w in result.warnings:
                    st.warning(w)

            # German Tax Annotations
            if german_tax_enabled:
                gt_config = GermanTaxConfig(
                    enabled=True,
                    filing_status=german_tax_filing,
                )
                gt_annotations = annotate_trades(result.trades, mapping, gt_config)
                st.divider()
                st.subheader("German Tax Advisory (InvStG)")
                if not gt_annotations:
                    st.info("No taxable trades -- German tax annotations not applicable.")
                else:
                    # PFIC warnings
                    pfic_annotations = [a for a in gt_annotations if a.pfic_risk]
                    for a in pfic_annotations:
                        st.warning(
                            f"PFIC risk: **{a.ticker}** is domiciled in {a.domicile}. "
                            "Non-US funds may trigger punitive IRS PFIC taxation."
                        )

                    # Teilfreistellung table
                    gt_rows = []
                    for a in gt_annotations:
                        gt_rows.append({
                            "Ticker": a.ticker,
                            "Category": a.fund_category.value,
                            "Teilfreistellung": f"{a.teilfreistellung_pct}%",
                            "PFIC Risk": "YES" if a.pfic_risk else "No",
                            "Domicile": a.domicile,
                            "Notes": "; ".join(a.notes),
                        })
                    st.dataframe(gt_rows, width="stretch", hide_index=True)

                    # Sparerpauschbetrag summary
                    summary = generate_summary(gt_annotations, german_tax_filing)
                    sparer = summary["sparerpauschbetrag_eur"]
                    st.caption(
                        f"Sparerpauschbetrag: EUR {sparer:,} ({german_tax_filing}). "
                        f"First EUR {sparer:,} of investment income is tax-free."
                    )

            # Download markdown report
            st.divider()
            st.subheader("Export")
            from rebalancer.output import write_markdown_report

            buf_path = _save_temp("", ".md")
            write_markdown_report(result, buf_path, output_config)
            md_content = buf_path.read_text()
            st.download_button(
                label="Download Markdown Report",
                data=md_content,
                file_name="rebalance_report.md",
                mime="text/markdown",
            )
