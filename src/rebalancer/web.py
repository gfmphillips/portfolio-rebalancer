"""Streamlit web GUI for the portfolio rebalancer."""

import tempfile
from decimal import Decimal, InvalidOperation
from pathlib import Path

import plotly.graph_objects as go
import streamlit as st
import yaml

from rebalancer.config import load_mapping, load_unified_config
from rebalancer.engine import CashPools, _build_initial_cash_pools, analyze_consolidation, build_run_metadata, rebalance
from rebalancer.fx import BankCashAccount, build_bank_cash_positions, convert_bank_cash_to_positions, fetch_fx_rate
from rebalancer.german_tax import annotate_trades, generate_summary
from rebalancer.models import (
    TAX_ADVANTAGED,
    AccountType,
    AllocationTarget,
    CashCategory,
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
    _format_currency,
    build_execution_plan,
    filter_actionable_trades,
    sort_trades,
)
from rebalancer.parser import attach_lots, parse_fidelity_csv, parse_fidelity_lots_paste, parse_lots, parse_transactions

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
    "dismiss_welcome": False,
    "accepted_disclaimer": False,
}
for key, default in _DEFAULTS.items():
    if key not in st.session_state:
        st.session_state[key] = default

# ---------------------------------------------------------------------------
# Sidebar
# ---------------------------------------------------------------------------

st.sidebar.title("Configuration")

# --- 1. Positions CSV ---
st.sidebar.header("Portfolio Data")
uploaded_csv = st.sidebar.file_uploader(
    "Upload Fidelity positions CSV", type=["csv"], key="csv_upload",
    help="Export from Fidelity: Positions page > Download > CSV. The parser handles Fidelity's formatting automatically (dollar signs, trailing ** on money market symbols, account type detection).",
)
use_example_csv = st.sidebar.checkbox(
    "Use example CSV", value=not uploaded_csv, key="use_example",
    help="Load a sample portfolio with multiple accounts and asset classes for demonstration.",
)

if uploaded_csv:
    csv_path = _save_temp(uploaded_csv.getvalue().decode("utf-8-sig"), ".csv")
elif use_example_csv and (EXAMPLE_DIR / "fidelity_positions.csv").exists():
    csv_path = EXAMPLE_DIR / "fidelity_positions.csv"
else:
    csv_path = None

# --- 2. Target Allocation ---
st.sidebar.header("Target Allocation")

_ALLOC_HELP = {
    "cash": "Brokerage cash (SPAXX, FDRXX). Set to 0 to deploy all idle brokerage cash into funds. Bank emergency funds should NOT be included here.",
    "bonds": "Fixed income (BND, VCSH, etc.). Provides stability and income.",
    "reit": "Real estate investment trusts (VNQ, VNQI). Set to 0 if not part of your IPS.",
    "us_equity": "US total market / S&P 500 (VTI, FXAIX, etc.). Core domestic equity exposure.",
    "intl_equity": "International stocks (VXUS, VGK, etc.). Diversification beyond US markets.",
}
alloc_values = {}
for ac in ASSET_CLASSES:
    default_val = {"cash": 0, "bonds": 20, "reit": 0, "us_equity": 48, "intl_equity": 32}.get(ac, 0)
    alloc_values[ac] = st.sidebar.number_input(
        ac,
        min_value=0,
        max_value=100,
        value=default_val,
        step=1,
        key=f"alloc_{ac}",
        help=_ALLOC_HELP.get(ac, ""),
    )

alloc_sum = sum(alloc_values.values())
if alloc_sum == 100:
    st.sidebar.success(f"Sum: {alloc_sum}%")
elif alloc_sum > 100:
    st.sidebar.error(f"Sum: {alloc_sum}% (must be 100)")
else:
    st.sidebar.warning(f"Sum: {alloc_sum}% (must be 100)")

# --- 3. Ticker Mapping ---
st.sidebar.header("Ticker Mapping")
default_mapping = _load_example_text("mapping.yaml")
mapping_text = st.sidebar.text_area(
    "mapping.yaml",
    value=st.session_state.mapping_text or default_mapping,
    height=200,
    key="mapping_input",
    help="Maps each ticker to an asset class. Also defines 'preferred' end-state funds (e.g. VTI) and 'consolidate_to' targets for legacy funds (e.g. FXAIX -> VTI). Add any tickers from your CSV that aren't already listed.",
)
st.session_state.mapping_text = mapping_text

# --- 4. Rebalance Settings ---
st.sidebar.header("Rebalance Settings")
threshold_pct = st.sidebar.number_input(
    "Absolute threshold %",
    min_value=0.0,
    max_value=50.0,
    value=5.0,
    step=0.5,
    key="threshold_pct",
    help="Rebalance if any asset class drifts more than this many percentage points from target. Example: with 5%, a 48% target triggers at 43% or 53%.",
)
threshold_relative_pct = st.sidebar.number_input(
    "Relative threshold %",
    min_value=0.0,
    max_value=100.0,
    value=20.0,
    step=1.0,
    key="threshold_relative_pct",
    help="Rebalance if drift exceeds this % of the target itself. Example: with 20%, a 20% target triggers at 16% or 24% (20% of 20 = 4pp). Catches drift in smaller allocations that the absolute band would miss. Either band breached = rebalance.",
)
min_trade_value = st.sidebar.number_input(
    "Min trade value ($)",
    min_value=0.0,
    value=500.0,
    step=50.0,
    key="min_trade_value",
    help="Ignore trades smaller than this dollar amount. Prevents generating tiny trades that aren't worth executing.",
)

# --- 5. Tax ---
st.sidebar.header("Tax")
tax_enabled = st.sidebar.toggle("Tax-aware trading", value=False, key="tax_enabled",
    help="When enabled: sells in retirement accounts first (no tax), prefers selling losses in taxable accounts (tax-loss harvesting), warns on taxable gains, and detects wash sales across accounts.",
)
uploaded_transactions = st.sidebar.file_uploader(
    "Transaction history CSV (optional)",
    type=["csv"],
    key="transactions_upload",
    help="Upload recent transaction history for wash sale detection. Supports simplified format (Date,Account,Ticker,Action,Shares) or Fidelity native export. Recent buys within 30 days are checked against proposed loss-sells.",
)
txn_path = None
if uploaded_transactions:
    txn_path = _save_temp(uploaded_transactions.getvalue().decode("utf-8-sig"), ".csv")

uploaded_lots = st.sidebar.file_uploader(
    "Tax lot CSV (optional)",
    type=["csv"],
    key="lots_upload",
    help="Upload tax lot data for lot-aware selling. Format: Account,Ticker,AcquisitionDate,Shares,CostBasisPerShare. Enables HIFO (taxable) and FIFO (retirement) lot selection.",
)
lots_path = None
if uploaded_lots:
    lots_path = _save_temp(uploaded_lots.getvalue().decode("utf-8-sig"), ".csv")

fidelity_lots_paste = st.sidebar.text_area(
    "Paste Fidelity lot data",
    value="",
    height=150,
    key="fidelity_lots_paste",
    help="Copy-paste the Fidelity Positions page (with lots expanded) as an alternative to uploading a lot CSV. If both are provided, the uploaded CSV takes priority.",
)

# --- 6. External Cash ---
st.sidebar.header("External Cash")

st.sidebar.subheader("Investable Cash")
st.sidebar.caption("Bank cash available for rebalancing. Counts toward allocation and can fund buys.")
invest_usd = st.sidebar.number_input(
    "Investable USD ($)",
    min_value=0.0,
    value=0.0,
    step=100.0,
    key="invest_usd",
    help="Cash held in US bank accounts available for investing. Brokerage cash (SPAXX, FDRXX) is already in your CSV.",
)
invest_eur = st.sidebar.number_input(
    "Investable EUR (\u20ac)",
    min_value=0.0,
    value=0.0,
    step=100.0,
    key="invest_eur",
    help="Cash held in European bank accounts available for investing.",
)

st.sidebar.subheader("Emergency Cash")
st.sidebar.caption("Emergency fund. Visible in portfolio total but excluded from rebalancing.")
emergency_usd = st.sidebar.number_input(
    "Emergency USD ($)",
    min_value=0.0,
    value=0.0,
    step=100.0,
    key="emergency_usd",
    help="US emergency fund. Shows in portfolio overview but the rebalancer won't touch it.",
)
emergency_eur = st.sidebar.number_input(
    "Emergency EUR (\u20ac)",
    min_value=0.0,
    value=0.0,
    step=100.0,
    key="emergency_eur",
    help="European emergency fund. Shows in portfolio overview but the rebalancer won't touch it.",
)

use_live_fx = st.sidebar.checkbox("Fetch live EUR/USD rate", value=True, key="use_live_fx",
    help="Fetches the current EUR/USD exchange rate from the Frankfurter API. Falls back to the manual rate if the API is unavailable.",
)

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
    help="How many USD per 1 EUR. Used when live rate is unavailable or unchecked. Example: 1.10 means 1 EUR = 1.10 USD.",
)

# --- 7. Display Settings ---
show_only_actionable = True
sort_order = [SortKey.SELLS_FIRST, SortKey.LARGEST_TRADE_FIRST]
currency_precision = 0

# --- 8. German Tax ---
st.sidebar.header("German Tax")
german_tax_enabled = st.sidebar.toggle(
    "Show German tax annotations", value=True, key="german_tax_enabled",
    help="Adds InvStG analysis to the Trade Plan: Teilfreistellung rates (30% for equity, 15% mixed, 0% bonds), PFIC risk warnings for non-US funds, and Sparerpauschbetrag reminder.",
)
german_tax_filing = "single"
if german_tax_enabled:
    german_tax_filing = st.sidebar.selectbox(
        "Filing status",
        options=["single", "married"],
        index=0,
        key="german_tax_filing",
        help="Determines the Sparerpauschbetrag: 1,000 EUR for single filers, 2,000 EUR for married filing jointly.",
    )

# --- 9. Account Types ---
with st.sidebar.expander("Account Types"):
    acct_mapping_text = ""
    for substr, acct_type in DEFAULT_ACCOUNT_MAPPINGS.items():
        acct_mapping_text += f"{substr}: {acct_type}\n"
    acct_yaml = st.text_area(
        "Account substring -> type",
        value=acct_mapping_text,
        height=150,
        key="acct_mapping_input",
        help="Maps substrings in Fidelity account names to tax treatment. If an account name contains 'ROTH', it's classified as roth_ira. This affects sell priority (retirement accounts are sold first) and tax impact calculations.",
    )

# --- 10. Advanced ---
with st.sidebar.expander("Advanced"):
    st.caption("Raw unified config YAML (read-only view / apply override)")

    # Build current config from widgets
    _current_unified = {
        "allocation": {ac: alloc_values[ac] for ac in ASSET_CLASSES},
        "rebalance": {
            "threshold_pct": threshold_pct,
            "min_trade_value": min_trade_value,
        },
        "cash": {
            "eurusd_fx": float(manual_fx),
            "investable": {"eur": float(invest_eur), "usd": float(invest_usd)},
            "emergency": {"eur": float(emergency_eur), "usd": float(emergency_usd)},
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

# --- Sidebar footer ---
st.sidebar.divider()
st.sidebar.markdown(
    "[Share feedback or request a feature]"
    "(https://forms.gle/8qMfAWQX9aiWZCo26)",
)
st.sidebar.caption("Built for Bogleheads. Free and open-source.")


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


def _build_targets() -> list[AllocationTarget]:
    """Build target allocation list from sidebar widget values."""
    if alloc_sum != 100:
        raise ValueError(f"Target allocations must sum to 100, got {alloc_sum}")
    return [
        AllocationTarget(asset_class=ac, target_pct=Decimal(str(alloc_values[ac])))
        for ac in ASSET_CLASSES
        if alloc_values[ac] > 0
    ]


def _build_config() -> tuple[RebalanceConfig, list[str]]:
    """Build RebalanceConfig from sidebar widgets, including account mappings.

    Returns (config, errors).
    """
    account_mappings: dict[str, AccountType] = {}
    errors: list[str] = []
    try:
        parsed_acct = yaml.safe_load(acct_yaml)
        if isinstance(parsed_acct, dict):
            valid_types = {e.value for e in AccountType}
            for substr, acct_type_str in parsed_acct.items():
                if acct_type_str in valid_types:
                    account_mappings[substr] = AccountType(acct_type_str)
                else:
                    errors.append(
                        f"Unknown account type '{acct_type_str}' for '{substr}'. "
                        f"Valid types: {', '.join(sorted(valid_types))}"
                    )
    except Exception as e:
        errors.append(f"Failed to parse account type mappings: {e}")

    config = RebalanceConfig(
        threshold_pct=Decimal(str(threshold_pct)),
        threshold_relative_pct=Decimal(str(threshold_relative_pct)),
        min_trade_value=Decimal(str(min_trade_value)),
        tlh_enabled=tax_enabled,
        avoid_gains_in_taxable=tax_enabled,
        cash_to_invest=Decimal("0"),
        account_mappings=account_mappings,
    )
    return config, errors


def _parse_lots_data(positions: list[Position]) -> tuple[list[str], list[str]]:
    """Parse and attach tax lots from CSV upload or Fidelity paste.

    Returns (lot_warnings, lot_errors).
    """
    lot_warnings: list[str] = []
    lot_errors: list[str] = []
    if lots_path is not None:
        try:
            lots_data = parse_lots(lots_path)
            lot_warnings = attach_lots(positions, lots_data)
        except Exception as e:
            lot_errors.append(f"Failed to parse tax lot CSV: {e}")
    elif fidelity_lots_paste and fidelity_lots_paste.strip():
        try:
            lots_data = parse_fidelity_lots_paste(fidelity_lots_paste)
            lot_warnings = attach_lots(positions, lots_data)
        except Exception as e:
            lot_errors.append(f"Failed to parse pasted Fidelity lot data: {e}")
    return lot_warnings, lot_errors


def _load_all():
    """Parse all inputs and return (positions, targets, mapping, config, output_config, bank_positions, recent_transactions)."""
    if csv_path is None:
        raise ValueError("No positions CSV provided. Upload a file or check 'Use example CSV'.")

    positions = parse_fidelity_csv(csv_path)
    if not positions:
        raise ValueError("No positions found in CSV. Check the file format.")

    targets = _build_targets()

    # Mapping
    map_path = _save_temp(mapping_text, ".yaml")
    mapping = load_mapping(map_path)

    config, acct_map_errors = _build_config()

    # Build bank cash positions using new CashConfig
    eur_usd_rate = Decimal(str(manual_fx))
    cash_config = CashConfig(
        eurusd_fx=eur_usd_rate,
        investable=CashCategory(
            eur=Decimal(str(invest_eur)),
            usd=Decimal(str(invest_usd)),
        ),
        emergency=CashCategory(
            eur=Decimal(str(emergency_eur)),
            usd=Decimal(str(emergency_usd)),
        ),
    )
    bank_positions = build_bank_cash_positions(cash_config)

    # Auto-register synthetic cash tickers in the mapping
    for bp in bank_positions:
        if bp.ticker not in mapping:
            mapping[bp.ticker] = TickerMapping(asset_class="cash")

    # Parse transaction history if uploaded
    recent_transactions = None
    txn_errors: list[str] = []
    if txn_path is not None:
        try:
            recent_transactions = parse_transactions(txn_path)
        except Exception as e:
            txn_errors.append(f"Failed to parse transaction history CSV: {e}")

    lot_warnings, lot_errors = _parse_lots_data(positions)

    all_errors = acct_map_errors + txn_errors + lot_errors
    return positions, targets, mapping, config, output_config, bank_positions, recent_transactions, lot_warnings, all_errors


# ---------------------------------------------------------------------------
# Main content
# ---------------------------------------------------------------------------

st.title("Portfolio Rebalancer")

# Disclaimer gate on first use
if not st.session_state.accepted_disclaimer:
    st.warning(
        "**Disclaimer:** This tool is for informational and educational purposes only. "
        "It does not constitute investment, financial, or tax advice. The developers are "
        "not registered investment advisors. All investments carry risk, including loss of "
        "principal. You are solely responsible for your investment decisions. "
        "See the full [disclaimer](https://github.com/gfmphillips/portfolio-rebalancer/blob/main/DISCLAIMER) "
        "for details."
    )
    accepted = st.checkbox(
        "I understand this tool does not provide financial advice and I accept all risks",
        key="disclaimer_checkbox",
    )
    if accepted:
        st.session_state.accepted_disclaimer = True
        st.rerun()
    else:
        st.stop()

# Welcome banner for new users
if not st.session_state.dismiss_welcome:
    with st.container():
        st.info(
            "**Welcome!** This tool analyzes your Fidelity portfolio and generates a "
            "step-by-step rebalance trade plan. Upload a positions CSV or use the "
            "example data to get started.\n\n"
            "**Sidebar options (left panel):**\n"
            "- **Portfolio Data** --- Upload your Fidelity CSV or use the example\n"
            "- **Target Allocation** --- Set your desired asset class mix (must sum to 100%)\n"
            "- **Ticker Mapping** --- Maps each ticker to an asset class. Add any tickers "
            "from your portfolio that aren't already listed\n"
            "- **Rebalance Settings** --- Drift thresholds and minimum trade size\n"
            "- **Tax** --- Enable tax-aware trading, upload transaction history for wash sale "
            "detection, or add tax lot data for lot-level selling\n"
            "- **External Cash** --- Add bank cash (investable or emergency) to include in "
            "the portfolio total\n\n"
            "**Getting started:** The example portfolio is loaded by default --- click the "
            "**Rebalance Analysis** tab to see it in action.\n\n"
            "**Privacy note:** If you upload your own data, your CSV may contain account "
            "names and numbers. This tool runs entirely in your browser session and "
            "**nothing is stored or transmitted**. However, if you want to be extra cautious, "
            "you can edit your CSV before uploading to remove or replace account names "
            "and account numbers (the tool only needs the ticker symbols, quantities, "
            "prices, and values to work).",
        )
        if st.button("Dismiss", key="dismiss_welcome_btn"):
            st.session_state.dismiss_welcome = True
            st.rerun()

# Tabs
tab_overview, tab_rebalance, tab_trades, tab_consolidation = st.tabs(
    ["Portfolio Overview", "Rebalance Analysis", "Trade Plan", "Consolidation"]
)

# Try to load data
try:
    positions, targets, mapping, config, oc, bank_positions, recent_txns, lot_warnings, lot_errors = _load_all()
    all_positions = positions + bank_positions
    data_ok = True
except Exception as e:
    data_ok = False
    data_error = str(e)
    lot_warnings = []
    lot_errors = []

if data_ok and lot_errors:
    for err in lot_errors:
        st.error(err)

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
        col1.metric("Total Portfolio Value", _format_currency(total_value, cur_prec),
                     help="Sum of all position market values across all accounts, including bank cash if enabled. This is the denominator used to calculate allocation percentages.")
        col2.metric("Accounts", len({p.account_name for p in all_positions}),
                     help="Number of distinct brokerage accounts detected in the CSV. Each account has a tax type (taxable, Roth, IRA, etc.) that affects trade priority.")
        col3.metric("Positions", len(all_positions),
                     help="Total number of individual holdings. Includes cash positions (SPAXX, FDRXX) and bank cash if enabled.")

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
                    "Price": _format_currency(p.price, cur_prec) if p.price else "-",
                    "Value": _format_currency(p.market_value, cur_prec),
                    "Cost Basis": _format_currency(p.cost_basis_total, cur_prec) if p.cost_basis_total is not None else "-",
                    "Gain/Loss": _format_currency(gain_loss, cur_prec) if gain_loss is not None else "-",
                    "Asset Class": asset_class,
                }
            )
        st.dataframe(pos_rows, width="stretch", hide_index=True)

        # Bank Cash Holdings summary
        if bank_positions:
            st.subheader("Bank Cash Holdings")
            cash_rows = []
            for bp in bank_positions:
                is_emergency = "EMERGENCY" in bp.ticker
                category = "Emergency" if is_emergency else "Investable"
                is_eur = "EUR" in bp.ticker
                if is_eur:
                    cash_rows.append({
                        "Category": category,
                        "Currency": "EUR",
                        "Original Amount": f"\u20ac{_dec(bp.quantity):,.2f}",
                        "USD Value": _format_currency(bp.market_value, cur_prec),
                        "Note": "Excluded from rebalancing" if is_emergency else "Counted in allocation",
                    })
                else:
                    cash_rows.append({
                        "Category": category,
                        "Currency": "USD",
                        "Original Amount": f"${_dec(bp.quantity):,.2f}",
                        "USD Value": _format_currency(bp.market_value, cur_prec),
                        "Note": "Excluded from rebalancing" if is_emergency else "Counted in allocation",
                    })
            st.dataframe(cash_rows, width="stretch", hide_index=True)


# ---- Tab 2: Rebalance Analysis ----
with tab_rebalance:
    if not data_ok:
        st.error(f"Cannot load data: {data_error}")
    else:
        metadata = build_run_metadata(eurusd_fx=Decimal(str(manual_fx)))
        result = rebalance(all_positions, targets, mapping, config, metadata=metadata, recent_transactions=recent_txns)
        for w in lot_warnings:
            result.warnings.append(w)
        st.session_state.result = result

        st.subheader("Allocation vs Target")
        st.caption("Drift = Current % minus Target %. Positive means overweight (sell), negative means underweight (buy). The orange dashed lines show the absolute threshold band.")

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

        # Comparison table with relative drift
        alloc_rows = []
        for cls in all_classes:
            current = result.current_allocation.get(cls, Decimal("0"))
            target = result.target_allocation.get(cls, Decimal("0"))
            drift = result.drift.get(cls, Decimal("0"))
            rel_drift = (_dec(abs(drift)) / _dec(target) * 100) if _dec(target) > 0 else 0.0
            alloc_rows.append(
                {
                    "Asset Class": cls,
                    "Current %": f"{_dec(current):.2f}",
                    "Target %": f"{_dec(target):.2f}",
                    "Drift (abs)": f"{_dec(drift):+.2f}pp",
                    "Drift (rel)": f"{rel_drift:.1f}%",
                }
            )
        st.dataframe(alloc_rows, width="stretch", hide_index=True)
        st.caption("**Drift (abs)** = percentage point difference from target. **Drift (rel)** = absolute drift as a % of the target itself. Either exceeding its threshold triggers a rebalance for that class.")

        # Decision checklist
        st.subheader("Decision Checklist")
        st.caption("Summary of whether action is needed and what the tax implications are. Rebalancing triggers when EITHER the absolute OR relative drift band is breached.")

        # Which classes breach bands?
        breached = []
        for cls in all_classes:
            abs_d = abs(_dec(result.drift.get(cls, Decimal("0"))))
            target = _dec(result.target_allocation.get(cls, Decimal("0")))
            rel_d = (abs_d / target * 100) if target > 0 else 0.0
            abs_breach = abs_d >= _dec(config.threshold_pct)
            rel_breach = rel_d >= _dec(config.threshold_relative_pct)
            if abs_breach or rel_breach:
                reasons = []
                if abs_breach:
                    reasons.append(f"{abs_d:.1f}pp abs")
                if rel_breach:
                    reasons.append(f"{rel_d:.0f}% rel")
                breached.append(f"**{cls}** ({', '.join(reasons)})")

        if breached:
            st.markdown(f"Bands breached: {', '.join(breached)}")
        else:
            st.success("All asset classes within both drift bands.")

        # Idle cash
        cash_positions = [p for p in all_positions if mapping.get(p.ticker) and mapping[p.ticker].asset_class == "cash"]
        total_cash = sum(_dec(p.market_value) for p in cash_positions)
        if total_cash > 0:
            st.markdown(f"Idle cash available: **{_format_currency(Decimal(str(total_cash)), cur_prec)}** across {len(cash_positions)} account(s)")

        # Trade breakdown by account type
        if result.trades:
            retirement_trades = [t for t in result.trades if t.account_type in TAX_ADVANTAGED]
            taxable_trades = [t for t in result.trades if t.account_type not in TAX_ADVANTAGED]
            st.markdown(
                f"Trades: **{len(retirement_trades)}** in retirement accounts, "
                f"**{len(taxable_trades)}** in taxable accounts"
            )
            if taxable_trades:
                taxable_sells = [t for t in taxable_trades if t.action == "SELL" and t.estimated_gain_loss is not None]
                net_gain = sum(_dec(t.estimated_gain_loss) for t in taxable_sells)
                if net_gain > 0:
                    st.warning(f"Taxable sells have estimated net gain of {_format_currency(Decimal(str(net_gain)), cur_prec)}")

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
                ti_col1.metric("Estimated Gains", _format_currency(ti.estimated_total_gains, cur_prec),
                               help="Total estimated capital gains from sells in taxable accounts. Computed as (market value - cost basis) x shares sold. Retirement account sells are excluded since they have no tax impact.")
                ti_col2.metric("Estimated Losses", _format_currency(ti.estimated_total_losses, cur_prec),
                               help="Total estimated capital losses from sells in taxable accounts. Losses can offset gains and reduce your tax bill. Up to $3,000 of net losses can offset ordinary income per year.")
                net_delta = "positive" if ti.estimated_net > 0 else "negative" if ti.estimated_net < 0 else None
                ti_col3.metric(
                    "Net",
                    _format_currency(ti.estimated_net, cur_prec),
                    delta=f"{_format_currency(ti.estimated_net, cur_prec)} taxable" if ti.estimated_net != 0 else None,
                    delta_color="inverse",
                    help="Gains minus losses. Positive = you owe taxes on this amount. Negative = you have a tax-loss harvesting benefit.",
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
                st.caption(f"{hidden_count} small trades hidden (below {_format_currency(Decimal(str(min_trade_value)), cur_prec)} threshold)")

            # --- Summary metrics ---
            m1, m2, m3 = st.columns(3)
            m1.metric("Sell first", f"{len(sell_steps)} trades", f"frees {_format_currency(total_sell, cur_prec)}",
                       help="Sells are executed first to free up cash. The tool prioritizes selling in retirement accounts (no tax) before taxable accounts, and prefers selling positions at a loss (tax-loss harvesting) over positions at a gain.")
            m2.metric("Then buy", f"{len(buy_steps)} trades", f"costs {_format_currency(total_buy, cur_prec)}",
                       help="Buys use cash freed from sells plus any existing idle cash. Each tax-advantaged account can only buy with its own cash (no transfers between IRAs). Taxable accounts share a single cash pool.")
            m3.metric("Total steps", len(steps),
                       help="Total number of individual trades to execute. Steps are ordered: all sells first (grouped by account), then all buys (grouped by account).")

            # --- How-to ---
            st.info(
                "**How to execute:** Work through each step in order. "
                "Complete all **sells** first to free up cash, then execute the **buys**. "
                "Steps are grouped by account so you can log into one account, complete its trades, then move on."
            )

            # --- Build cash pool tracking ---
            pools = _build_initial_cash_pools(all_positions, mapping)

            def _pool_label(acct_name: str, acct_type: AccountType) -> str:
                if acct_type in TAX_ADVANTAGED:
                    return acct_name
                return "Taxable (shared)"

            def _pool_balance(acct_name: str, acct_type: AccountType) -> Decimal:
                return pools.available(acct_name, acct_type)

            def _format_pool_state(acct_name: str, acct_type: AccountType) -> str:
                label = _pool_label(acct_name, acct_type)
                bal = _pool_balance(acct_name, acct_type)
                return f"{label}: {_format_currency(bal, cur_prec)}"

            # --- Starting cash summary ---
            st.divider()
            st.markdown("### Cash Sources")
            st.caption("Starting cash available per pool before any trades. Taxable accounts share one pool. Each retirement account is isolated.")
            pool_rows = []
            # Taxable pool
            if pools.taxable_pool > 0:
                taxable_accts = sorted({p.account_name for p in all_positions
                                        if p.account_type not in TAX_ADVANTAGED
                                        and mapping.get(p.ticker) and mapping[p.ticker].asset_class == "cash"
                                        and p.market_value > 0})
                pool_rows.append({
                    "Pool": "Taxable (shared)",
                    "Starting Cash": _format_currency(pools.taxable_pool, cur_prec),
                    "Accounts": ", ".join(taxable_accts) if taxable_accts else "-",
                    "Note": "Cash moves freely between taxable accounts",
                })
            for acct_name, bal in sorted(pools.tax_adv_pools.items()):
                if bal > 0:
                    pool_rows.append({
                        "Pool": acct_name,
                        "Starting Cash": _format_currency(bal, cur_prec),
                        "Accounts": acct_name,
                        "Note": "Isolated — can only buy within this account",
                    })
            if pool_rows:
                st.dataframe(pool_rows, width="stretch", hide_index=True)
            else:
                st.caption("No starting cash in any account.")

            # --- Sell phase ---
            if sell_steps:
                st.divider()
                st.markdown(f"### Phase 1: Sells ({len(sell_steps)} trades across {len(sell_accounts)} accounts)")
                st.caption("Sell overweight positions to free up cash for rebalancing. Proceeds are credited to the account's cash pool.")

                current_account = None
                for s in sell_steps:
                    t = s.trade
                    if t.account_name != current_account:
                        current_account = t.account_name
                        acct_sells = [x for x in sell_steps if x.trade.account_name == current_account]
                        acct_total = sum(x.trade.estimated_value for x in acct_sells)
                        st.markdown(
                            f"**{current_account}** ({t.account_type.value}) "
                            f"--- {len(acct_sells)} trade(s), {_format_currency(acct_total, cur_prec)} total"
                        )

                    # Credit proceeds to pool
                    pools.add(t.account_name, t.account_type, t.estimated_value)

                    col_step, col_detail = st.columns([1, 11])
                    with col_step:
                        st.markdown(f"#### {s.step_num}")
                    with col_detail:
                        gain_loss_str = ""
                        if t.estimated_gain_loss is not None:
                            gl = t.estimated_gain_loss
                            if gl > 0:
                                gain_loss_str = f" | Est. gain: {_format_currency(gl, cur_prec)}"
                            elif gl < 0:
                                gain_loss_str = f" | Est. loss: {_format_currency(gl, cur_prec)}"
                        st.markdown(
                            f"SELL **{t.ticker}** --- "
                            f"**{_dec(t.shares):.3f}** shares --- "
                            f"**{_format_currency(t.estimated_value, cur_prec)}**{gain_loss_str}"
                        )
                        pool_label = _pool_label(t.account_name, t.account_type)
                        pool_bal = _pool_balance(t.account_name, t.account_type)
                        st.caption(
                            f"{t.reasoning} | "
                            f"+{_format_currency(t.estimated_value, cur_prec)} to **{pool_label}** "
                            f"(now {_format_currency(pool_bal, cur_prec)})"
                        )
                        for w in t.warnings:
                            st.warning(w)

            # --- Buy phase ---
            if buy_steps:
                st.divider()
                st.markdown(f"### Phase 2: Buys ({len(buy_steps)} trades across {len(buy_accounts)} accounts)")
                st.caption("Use the freed cash to buy into underweight positions. Each buy draws from its account's cash pool.")

                current_account = None
                for s in buy_steps:
                    t = s.trade
                    if t.account_name != current_account:
                        current_account = t.account_name
                        acct_buys = [x for x in buy_steps if x.trade.account_name == current_account]
                        acct_total = sum(x.trade.estimated_value for x in acct_buys)
                        pool_before = _pool_balance(t.account_name, t.account_type)
                        st.markdown(
                            f"**{current_account}** ({t.account_type.value}) "
                            f"--- {len(acct_buys)} trade(s), {_format_currency(acct_total, cur_prec)} total "
                            f"--- {_format_currency(pool_before, cur_prec)} available in pool"
                        )

                    # Spend from pool
                    pools.spend(t.account_name, t.account_type, t.estimated_value)

                    col_step, col_detail = st.columns([1, 11])
                    with col_step:
                        st.markdown(f"#### {s.step_num}")
                    with col_detail:
                        pool_label = _pool_label(t.account_name, t.account_type)
                        pool_bal = _pool_balance(t.account_name, t.account_type)
                        st.markdown(
                            f"BUY **{t.ticker}** --- "
                            f"**{_dec(t.shares):.3f}** shares --- "
                            f"**{_format_currency(t.estimated_value, cur_prec)}**"
                        )
                        st.caption(
                            f"{t.reasoning} | "
                            f"-{_format_currency(t.estimated_value, cur_prec)} from **{pool_label}** "
                            f"(now {_format_currency(pool_bal, cur_prec)})"
                        )

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
            from rebalancer.output import write_csv_report, write_markdown_report

            buf_path = _save_temp("", ".md")
            write_markdown_report(result, buf_path, output_config)
            md_content = buf_path.read_text()

            csv_buf_path = _save_temp("", ".csv")
            write_csv_report(result, csv_buf_path, output_config)
            csv_content = csv_buf_path.read_text()

            dl_col1, dl_col2 = st.columns(2)
            with dl_col1:
                st.download_button(
                    label="Download Markdown Report",
                    data=md_content,
                    file_name="rebalance_report.md",
                    mime="text/markdown",
                )
            with dl_col2:
                st.download_button(
                    label="Download CSV Report",
                    data=csv_content,
                    file_name="trade_plan.csv",
                    mime="text/csv",
                )


# ---- Tab 4: Consolidation ----
with tab_consolidation:
    if not data_ok:
        st.error(f"Cannot load data: {data_error}")
    else:
        consolidation = analyze_consolidation(all_positions, mapping)

        # Progress metric
        st.subheader("Three-Fund Consolidation Progress")
        st.caption("Tracks your progress from multiple legacy funds toward a simplified end-state portfolio. Cash positions are excluded from this calculation since they are not part of the consolidation target.")
        col1, col2 = st.columns(2)
        col1.metric(
            "End-State Funds",
            _format_currency(consolidation.end_state_value, cur_prec),
            f"{consolidation.end_state_pct}%",
            help="Value held in funds marked 'preferred: true' in the mapping (e.g. VTI, VXUS, BND). These are your target holdings. The goal is to get this to 100%.",
        )
        col2.metric(
            "Legacy Funds",
            _format_currency(consolidation.legacy_value, cur_prec),
            f"{consolidation.legacy_pct}%",
            help="Value held in all other non-cash funds. These have a 'consolidate_to' target in the mapping. Consolidate them over time as tax-efficient opportunities arise.",
        )

        # Progress bar
        if consolidation.end_state_value + consolidation.legacy_value > 0:
            st.progress(_dec(consolidation.end_state_pct) / 100.0, text=f"{consolidation.end_state_pct}% consolidated")

        if not consolidation.opportunities:
            st.success("All non-cash positions are in end-state funds.")
        else:
            st.divider()
            st.subheader("Consolidation Opportunities")

            # Separate safe vs wait
            safe_opps = [o for o in consolidation.opportunities if o.safe_to_consolidate]
            wait_opps = [o for o in consolidation.opportunities if not o.safe_to_consolidate]

            if safe_opps:
                st.markdown("#### Free to Execute")
                st.caption("These positions can be consolidated now with no tax cost. Retirement accounts have no tax on sells. Taxable positions at a loss generate a tax benefit when sold.")
                safe_rows = []
                for opp in safe_opps:
                    gl_str = _format_currency(opp.estimated_gain_loss, cur_prec) if opp.estimated_gain_loss is not None else "-"
                    safe_rows.append({
                        "Ticker": opp.ticker,
                        "Account": opp.account_name,
                        "Value": _format_currency(opp.market_value, cur_prec),
                        "Consolidate To": opp.consolidate_to,
                        "Gain/Loss": gl_str,
                        "Reason": opp.reason,
                    })
                st.dataframe(safe_rows, width="stretch", hide_index=True)

            if wait_opps:
                st.markdown("#### Wait")
                st.caption("These positions are at a gain in taxable accounts. Selling would trigger capital gains tax. Wait for a market dip (position goes to a loss) or a spending need where you'd sell anyway.")
                wait_rows = []
                for opp in wait_opps:
                    gl_str = _format_currency(opp.estimated_gain_loss, cur_prec) if opp.estimated_gain_loss is not None else "-"
                    wait_rows.append({
                        "Ticker": opp.ticker,
                        "Account": opp.account_name,
                        "Value": _format_currency(opp.market_value, cur_prec),
                        "Consolidate To": opp.consolidate_to,
                        "Gain/Loss": gl_str,
                        "Reason": opp.reason,
                    })
                st.dataframe(wait_rows, width="stretch", hide_index=True)
