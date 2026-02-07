"""Streamlit web GUI for the portfolio rebalancer."""

import tempfile
from decimal import Decimal, InvalidOperation
from pathlib import Path

import plotly.graph_objects as go
import streamlit as st
import yaml

from .config import load_config, load_mapping, load_targets
from .engine import rebalance
from .models import AccountType, RebalanceConfig, RebalanceResult
from .output import _compute_allocation
from .parser import parse_fidelity_csv

# ---------------------------------------------------------------------------
# Page config
# ---------------------------------------------------------------------------
st.set_page_config(
    page_title="Portfolio Rebalancer",
    page_icon="\u2696\ufe0f",
    layout="wide",
)

# ---------------------------------------------------------------------------
# Session state defaults
# ---------------------------------------------------------------------------
for key, default in [
    ("positions", None),
    ("mapping_data", None),
    ("targets_data", None),
    ("config_data", None),
    ("result", None),
    ("mapping_text", None),
    ("targets_text", None),
    ("config_text", None),
]:
    if key not in st.session_state:
        st.session_state[key] = default


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

EXAMPLE_DIR = Path(__file__).parent.parent.parent / "examples"


def _load_example_text(name: str) -> str:
    p = EXAMPLE_DIR / name
    if p.exists():
        return p.read_text()
    return ""


def _dec(val: Decimal) -> float:
    """Convert Decimal to float for display."""
    return float(val)


def _format_money(val: Decimal) -> str:
    return f"${_dec(val):,.2f}"


def _save_temp(content: str, suffix: str) -> Path:
    """Write string content to a temp file and return the path."""
    tmp = tempfile.NamedTemporaryFile(mode="w", suffix=suffix, delete=False)
    tmp.write(content)
    tmp.flush()
    return Path(tmp.name)


# ---------------------------------------------------------------------------
# Sidebar — file uploads + configuration
# ---------------------------------------------------------------------------

st.sidebar.title("Configuration")

# --- Positions CSV ---
st.sidebar.header("1. Positions CSV")
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

# --- Targets ---
st.sidebar.header("2. Target Allocation")
default_targets = _load_example_text("targets.yaml")
targets_text = st.sidebar.text_area(
    "targets.yaml",
    value=st.session_state.targets_text or default_targets,
    height=150,
    key="targets_input",
)
st.session_state.targets_text = targets_text

# --- Mapping ---
st.sidebar.header("3. Ticker Mapping")
default_mapping = _load_example_text("mapping.yaml")
mapping_text = st.sidebar.text_area(
    "mapping.yaml",
    value=st.session_state.mapping_text or default_mapping,
    height=200,
    key="mapping_input",
)
st.session_state.mapping_text = mapping_text

# --- Config ---
st.sidebar.header("4. Rebalance Settings")
default_config = _load_example_text("config.yaml")
config_text = st.sidebar.text_area(
    "config.yaml",
    value=st.session_state.config_text or default_config,
    height=200,
    key="config_input",
)
st.session_state.config_text = config_text

# --- Cash to invest override ---
st.sidebar.header("5. New Cash to Invest")
cash_override = st.sidebar.number_input(
    "Override cash_to_invest ($)",
    min_value=0.0,
    value=0.0,
    step=100.0,
    key="cash_override",
)

# ---------------------------------------------------------------------------
# Load data
# ---------------------------------------------------------------------------


def _load_all():
    """Parse all inputs and return (positions, targets, mapping, config) or raise."""
    if csv_path is None:
        raise ValueError("No positions CSV provided. Upload a file or check 'Use example CSV'.")

    positions = parse_fidelity_csv(csv_path)
    if not positions:
        raise ValueError("No positions found in CSV. Check the file format.")

    tgt_path = _save_temp(targets_text, ".yaml")
    targets = load_targets(tgt_path)

    map_path = _save_temp(mapping_text, ".yaml")
    mapping = load_mapping(map_path)

    cfg_path = _save_temp(config_text, ".yaml")
    config = load_config(cfg_path)

    if cash_override > 0:
        config.cash_to_invest = Decimal(str(cash_override))

    return positions, targets, mapping, config


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
    positions, targets, mapping, config = _load_all()
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
            positions, mapping
        )

        # KPI row
        col1, col2, col3 = st.columns(3)
        col1.metric("Total Portfolio Value", _format_money(total_value))
        col2.metric("Accounts", len({p.account_name for p in positions}))
        col3.metric("Positions", len(positions))

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
            st.plotly_chart(fig, use_container_width=True)

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
            st.plotly_chart(fig2, use_container_width=True)

        # Positions table
        st.subheader("Positions")
        pos_rows = []
        for p in positions:
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
                    "Price": _format_money(p.price) if p.price else "-",
                    "Value": _format_money(p.market_value),
                    "Cost Basis": _format_money(p.cost_basis_total) if p.cost_basis_total is not None else "-",
                    "Gain/Loss": _format_money(gain_loss) if gain_loss is not None else "-",
                    "Asset Class": asset_class,
                }
            )
        st.dataframe(pos_rows, use_container_width=True, hide_index=True)


# ---- Tab 2: Rebalance Analysis ----
with tab_rebalance:
    if not data_ok:
        st.error(f"Cannot load data: {data_error}")
    else:
        result = rebalance(positions, targets, mapping, config)
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
        st.plotly_chart(fig3, use_container_width=True)

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
        st.dataframe(alloc_rows, use_container_width=True, hide_index=True)

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
        st.plotly_chart(fig4, use_container_width=True)

        # Tax impact
        ti = result.tax_impact
        if ti.taxable_trades_count > 0:
            st.subheader("Estimated Tax Impact (Taxable Accounts)")
            ti_col1, ti_col2, ti_col3 = st.columns(3)
            ti_col1.metric("Estimated Gains", _format_money(ti.estimated_total_gains))
            ti_col2.metric("Estimated Losses", _format_money(ti.estimated_total_losses))
            net_delta = "positive" if ti.estimated_net > 0 else "negative" if ti.estimated_net < 0 else None
            ti_col3.metric(
                "Net",
                _format_money(ti.estimated_net),
                delta=f"{_format_money(ti.estimated_net)} taxable" if ti.estimated_net != 0 else None,
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
            st.subheader(f"Recommended Trades ({len(result.trades)})")

            # Summary metrics
            sells = [t for t in result.trades if t.action == "SELL"]
            buys = [t for t in result.trades if t.action == "BUY"]
            total_sell = sum(t.estimated_value for t in sells)
            total_buy = sum(t.estimated_value for t in buys)

            m1, m2, m3 = st.columns(3)
            m1.metric("Sell Trades", len(sells), _format_money(total_sell))
            m2.metric("Buy Trades", len(buys), _format_money(total_buy))
            m3.metric("Total Trades", len(result.trades))

            st.divider()

            # Trade table
            trade_rows = []
            for t in result.trades:
                row = {
                    "Account": t.account_name,
                    "Action": t.action,
                    "Ticker": t.ticker,
                    "Shares": f"{_dec(t.shares):.3f}",
                    "Est. Value": _format_money(t.estimated_value),
                    "Gain/Loss": _format_money(t.estimated_gain_loss) if t.estimated_gain_loss is not None else "-",
                    "Reasoning": t.reasoning,
                }
                if t.warnings:
                    row["Warnings"] = "; ".join(t.warnings)
                else:
                    row["Warnings"] = ""
                trade_rows.append(row)
            st.dataframe(trade_rows, use_container_width=True, hide_index=True)

            # Trade breakdown chart
            st.subheader("Trade Values by Ticker")
            tickers = list({t.ticker for t in result.trades})
            sell_vals = []
            buy_vals = []
            for ticker in tickers:
                sv = sum(_dec(t.estimated_value) for t in sells if t.ticker == ticker)
                bv = sum(_dec(t.estimated_value) for t in buys if t.ticker == ticker)
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
            st.plotly_chart(fig5, use_container_width=True)

            # Warnings
            if result.warnings:
                st.subheader("Warnings")
                for w in result.warnings:
                    st.warning(w)

            # Download markdown report
            st.divider()
            st.subheader("Export")
            from io import StringIO

            from .output import write_markdown_report

            buf_path = _save_temp("", ".md")
            write_markdown_report(result, buf_path)
            md_content = buf_path.read_text()
            st.download_button(
                label="Download Markdown Report",
                data=md_content,
                file_name="rebalance_report.md",
                mime="text/markdown",
            )
