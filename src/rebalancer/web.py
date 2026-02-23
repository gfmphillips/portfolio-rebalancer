"""Streamlit web GUI for the portfolio rebalancer."""

import tempfile
from datetime import datetime
from decimal import Decimal, InvalidOperation
from pathlib import Path

import plotly.graph_objects as go
import streamlit as st
import pandas as pd
import yaml

from rebalancer.config import load_mapping, load_unified_config
from rebalancer.prices import fetch_prices
from rebalancer.engine import CashPools, EMERGENCY_TICKERS, _build_initial_cash_pools, analyze_consolidation, build_run_metadata, project_positions, rebalance
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
    Position,
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
# Temp file cleanup
# Runs at the start of every render cycle so that sensitive financial data
# written to disk during the previous render is removed before the new one.
# ---------------------------------------------------------------------------
for _stale_path in st.session_state.pop("_temp_paths", []):
    try:
        Path(_stale_path).unlink(missing_ok=True)
    except Exception:
        pass

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------
EXAMPLE_DIR = Path(__file__).parent.parent.parent / "examples"

ASSET_CLASSES = ["cash", "bonds", "reit", "us_equity", "intl_equity"]

ASSET_CLASS_COLORS = {
    "cash": "#94a3b8",
    "bonds": "#60a5fa",
    "reit": "#f59e0b",
    "us_equity": "#22c55e",
    "intl_equity": "#8b5cf6",
    "unmapped": "#ef4444",
}
_FALLBACK_COLORS = ["#64748b", "#3b82f6", "#f97316", "#84cc16", "#a855f7", "#ec4899"]


def _pie_colors(labels: list[str]) -> list[str]:
    """Return a color per label using a consistent asset-class palette."""
    return [
        ASSET_CLASS_COLORS.get(label, _FALLBACK_COLORS[i % len(_FALLBACK_COLORS)])
        for i, label in enumerate(labels)
    ]


# Human-readable names for synthetic cash tickers injected at runtime
_SYNTHETIC_TICKER_LABELS = {
    "CASH-USD-INVESTABLE": "US Bank Cash",
    "CASH-EUR-INVESTABLE": "EUR Bank Cash",
    "CASH-USD-EMERGENCY": "US Emergency Fund",
    "CASH-EUR-EMERGENCY": "EUR Emergency Fund",
}


def _build_pie_hover_texts(
    labels: list[str],
    positions: list,
    mapping: dict,
    precision: int,
) -> list[str]:
    """Build per-ticker breakdown hover text for each asset class in a holdings pie chart.

    Returns one HTML string per label, suitable for use as Plotly customdata.
    """
    by_class: dict[str, dict[str, Decimal]] = {}
    for p in positions:
        info = mapping.get(p.ticker)
        ac = info.asset_class if info else "unmapped"
        by_class.setdefault(ac, {})
        by_class[ac][p.ticker] = by_class[ac].get(p.ticker, Decimal("0")) + p.market_value

    texts = []
    for label in labels:
        tickers = by_class.get(label, {})
        if not tickers:
            texts.append("No positions")
            continue
        bucket_total = sum(tickers.values())
        lines = []
        for ticker, value in sorted(tickers.items(), key=lambda x: x[1], reverse=True):
            pct = float(value / bucket_total * 100) if bucket_total > 0 else 0.0
            display = _SYNTHETIC_TICKER_LABELS.get(ticker, ticker)
            lines.append(
                f"{display}:  {pct:.0f}% of class  ·  {_format_currency(value, precision)}"
            )
        texts.append("<br>".join(lines))
    return texts


def _build_target_pie_hover_texts(
    labels: list[str],
    mapping: dict,
) -> list[str]:
    """Build hover text for the target allocation pie showing which funds map to each class."""
    by_class: dict[str, list[str]] = {}
    for ticker, info in mapping.items():
        if ticker.startswith("CASH-"):  # skip synthetic runtime tickers
            continue
        ac = info.asset_class
        by_class.setdefault(ac, [])
        if info.preferred:
            by_class[ac].insert(0, f"{ticker} (your target fund)")
        elif info.consolidate_to:
            by_class[ac].append(f"{ticker}  →  {info.consolidate_to}")
        else:
            by_class[ac].append(ticker)

    texts = []
    for label in labels:
        funds = by_class.get(label, [])
        if funds:
            texts.append("Funds in this class:<br>" + "<br>".join(f"  {f}" for f in funds))
        else:
            texts.append("No funds mapped to this class")
    return texts


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

ASSET_CLASS_DISPLAY = {
    "cash": "Cash",
    "bonds": "Bonds",
    "reit": "REIT",
    "us_equity": "US Equity",
    "intl_equity": "Intl Equity",
}
ASSET_CLASS_OPTIONS = list(ASSET_CLASS_DISPLAY.values())
ASSET_CLASS_OPTION_TO_KEY = {v: k for k, v in ASSET_CLASS_DISPLAY.items()}

ACCOUNT_TYPE_LABELS = {
    "taxable": "Taxable (Individual / Brokerage)",
    "traditional_ira": "Traditional IRA / Rollover IRA",
    "roth_ira": "Roth IRA",
    "roth_401k": "Roth 401(k)",
    "401k": "Traditional 401(k)",
    "hsa": "HSA",
}
ACCOUNT_TYPE_LABEL_TO_VALUE = {v: k for k, v in ACCOUNT_TYPE_LABELS.items()}

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
    """Write string content to a temp file and return the path.

    The path is registered in session state and automatically deleted at the
    start of the next render cycle, so uploaded financial data never
    accumulates on disk.
    """
    tmp = tempfile.NamedTemporaryFile(mode="w", suffix=suffix, delete=False)
    tmp.write(content)
    tmp.flush()
    p = Path(tmp.name)
    st.session_state.setdefault("_temp_paths", []).append(str(p))
    return p


def _default_ticker_rows() -> list[dict]:
    """Load default ticker rows from examples/mapping.yaml."""
    try:
        m = load_mapping(EXAMPLE_DIR / "mapping.yaml")
        return [
            {
                "Ticker": ticker,
                "Asset Class": ASSET_CLASS_DISPLAY.get(info.asset_class, info.asset_class),
                "Target Fund?": info.preferred,
                "Consolidate Into": info.consolidate_to or "",
                "Price ($)": float(info.price) if info.price is not None else None,
            }
            for ticker, info in m.items()
        ]
    except Exception:
        return []


def _default_acct_rules() -> list[dict]:
    """Build default account rules from DEFAULT_ACCOUNT_MAPPINGS."""
    return [
        {"If name contains": substr, "Treat as": ACCOUNT_TYPE_LABELS.get(acct_type, acct_type)}
        for substr, acct_type in DEFAULT_ACCOUNT_MAPPINGS.items()
    ]


def _build_mapping_from_rows(rows: list[dict]) -> dict[str, TickerMapping]:
    """Build a TickerMapping dict from the structured ticker editor rows."""
    mapping: dict[str, TickerMapping] = {}
    for row in rows:
        ticker = str(row.get("Ticker") or "").strip().upper()
        if not ticker:
            continue
        asset_class_display = str(row.get("Asset Class") or "US Equity")
        asset_class = ASSET_CLASS_OPTION_TO_KEY.get(asset_class_display, "us_equity")
        preferred = bool(row.get("Target Fund?", False))
        consolidate_to = str(row.get("Consolidate Into") or "").strip() or None
        price_val = row.get("Price ($)")
        try:
            price = Decimal(str(price_val)) if price_val is not None and str(price_val) not in ("", "nan", "None") else None
        except Exception:
            price = None
        mapping[ticker] = TickerMapping(
            asset_class=asset_class,
            preferred=preferred,
            consolidate_to=consolidate_to,
            price=price,
        )
    return mapping


def _build_account_mappings_from_rows(rules: list[dict]) -> dict[str, AccountType]:
    """Build an account_mappings dict from the structured account rules editor rows."""
    account_mappings: dict[str, AccountType] = {}
    valid_values = {e.value for e in AccountType}
    for rule in rules:
        keyword = str(rule.get("If name contains") or "").strip()
        acct_label = str(rule.get("Treat as") or "")
        acct_value = ACCOUNT_TYPE_LABEL_TO_VALUE.get(acct_label)
        if keyword and acct_value and acct_value in valid_values:
            account_mappings[keyword] = AccountType(acct_value)
    return account_mappings


# ---------------------------------------------------------------------------
# Session state defaults
# ---------------------------------------------------------------------------
_DEFAULTS = {
    "positions": None,
    "mapping_data": None,
    "result": None,
    "dismiss_welcome": False,
    "accepted_disclaimer": False,
}
for key, default in _DEFAULTS.items():
    if key not in st.session_state:
        st.session_state[key] = default

if "ticker_rows" not in st.session_state:
    st.session_state.ticker_rows = _default_ticker_rows()
if "acct_rules" not in st.session_state:
    st.session_state.acct_rules = _default_acct_rules()
if "live_prices" not in st.session_state:
    st.session_state.live_prices = {}
if "price_timestamp" not in st.session_state:
    st.session_state.price_timestamp = None
if "price_tickers" not in st.session_state:
    st.session_state.price_tickers = set()
if "live_fx_rate" not in st.session_state:
    st.session_state.live_fx_rate = None  # cached EUR/USD rate; None = not yet fetched
if "manual_fx" not in st.session_state:
    st.session_state.manual_fx = 1.10  # default before first live fetch
# Content hashes for uploaded files — used to avoid re-parsing on every rerun.
if "txn_bytes_hash" not in st.session_state:
    st.session_state.txn_bytes_hash = None
if "lots_bytes_hash" not in st.session_state:
    st.session_state.lots_bytes_hash = None

# ---------------------------------------------------------------------------
# Sidebar
# ---------------------------------------------------------------------------

st.sidebar.title("Configuration")

# --- 1. Portfolio Data ---
st.sidebar.header("1. Portfolio Data")
uploaded_csv = st.sidebar.file_uploader(
    "Upload Fidelity positions CSV", type=["csv"], key="csv_upload",
    help="Export from Fidelity: Positions page > Download > CSV.",
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

_price_col1, _price_col2 = st.sidebar.columns([3, 1])
with _price_col1:
    _pts = st.session_state.price_timestamp
    _np = len(st.session_state.live_prices)
    if _pts:
        st.caption(f"Live prices: {_np} tickers · updated {_pts}")
    else:
        st.caption("Live prices: loading...")
with _price_col2:
    _refresh_prices = st.button("↺ Refresh", key="refresh_prices", use_container_width=True)

# --- 2. Your Accounts ---
st.sidebar.header("2. Your Accounts")
st.sidebar.caption(
    "Each row maps a word in your account name to a tax type. For example, if your account "
    "is called 'Roth IRA — Individual,' enter 'Roth' and select 'Roth IRA.' The tool uses "
    "this to decide where to place trades for the best tax outcome. Use ➕ to add new rules."
)
acct_df_edited = st.sidebar.data_editor(
    pd.DataFrame(st.session_state.acct_rules),
    column_config={
        "If name contains": st.column_config.TextColumn(
            "If name contains...",
            required=True,
            help="A word that appears in your Fidelity account name, e.g. 'ROTH' or 'Rollover'.",
        ),
        "Treat as": st.column_config.SelectboxColumn(
            "Treat as",
            options=list(ACCOUNT_TYPE_LABELS.values()),
            required=True,
            help="The tax treatment for accounts matching this keyword.",
        ),
    },
    num_rows="dynamic",
    hide_index=True,
    use_container_width=True,
    key="acct_editor",
)
st.session_state.acct_rules = acct_df_edited.to_dict("records")
account_mappings = _build_account_mappings_from_rows(st.session_state.acct_rules)

# --- 3. Target Mix ---
st.sidebar.header("3. Target Mix")
st.sidebar.caption("Set your desired allocation. Must add up to 100%.")
_ALLOC_HELP = {
    "cash": "Brokerage cash (SPAXX, FDRXX). Set to 0 to invest all idle cash into funds.",
    "bonds": "Fixed income (BND, VCSH, etc.). Provides stability and income.",
    "reit": "Real estate investment trusts (VNQ, VNQI). Set to 0 to skip.",
    "us_equity": "US stocks (VTI, FXAIX, etc.). Core domestic equity exposure.",
    "intl_equity": "International stocks (VXUS, VGK, etc.). Diversification beyond US markets.",
}
alloc_values = {}
for ac in ASSET_CLASSES:
    default_val = {"cash": 0, "bonds": 20, "reit": 0, "us_equity": 48, "intl_equity": 32}.get(ac, 0)
    alloc_values[ac] = st.sidebar.number_input(
        ASSET_CLASS_DISPLAY.get(ac, ac),
        min_value=0,
        max_value=100,
        value=default_val,
        step=1,
        key=f"alloc_{ac}",
        help=_ALLOC_HELP.get(ac, ""),
    )

alloc_sum = sum(alloc_values.values())
if alloc_sum == 100:
    st.sidebar.success(f"Total: {alloc_sum}%")
elif alloc_sum > 100:
    st.sidebar.error(f"Total: {alloc_sum}% — must equal 100%")
else:
    st.sidebar.warning(f"Total: {alloc_sum}% — must equal 100%")

# --- 4. Fund Classification ---
st.sidebar.header("4. Fund Classification")
st.sidebar.caption(
    "Classify each fund so the tool knows what asset class it represents and where to direct "
    "new money. If a fund from your portfolio is missing, add it with ➕. Any fund left "
    "unclassified will appear as a warning in the results."
)
_ticker_df = pd.DataFrame(st.session_state.ticker_rows)
_live_prices = st.session_state.live_prices
_ticker_df["Live Price"] = _ticker_df["Ticker"].apply(
    lambda t: float(_live_prices[str(t).strip().upper()])
    if t and str(t).strip().upper() in _live_prices else None
)
ticker_df_edited = st.sidebar.data_editor(
    _ticker_df,
    column_config={
        "Ticker": st.column_config.TextColumn(
            "Ticker",
            required=True,
            help="The fund's ticker symbol, e.g. VTI, VXUS, SPAXX.",
        ),
        "Asset Class": st.column_config.SelectboxColumn(
            "Asset Class",
            options=ASSET_CLASS_OPTIONS,
            required=True,
            help="What type of asset this fund holds.",
        ),
        "Target Fund?": st.column_config.CheckboxColumn(
            "Target Fund?",
            help="Mark this fund as your preferred long-term holding for its asset class. When the tool needs to buy more of that asset class, it will buy this fund. Only one fund per asset class should be marked.",
        ),
        "Consolidate Into": st.column_config.TextColumn(
            "Consolidate Into",
            help="If you're phasing out this fund, type the ticker of the replacement fund here (e.g., type VTI if you want to gradually shift out of FXAIX). The tool will sell this fund when rebalancing instead of your target fund.",
        ),
        "Price ($)": st.column_config.NumberColumn(
            "Price ($)",
            help="The current share price of this fund. Only required if this is a target fund that you do not yet own — the tool needs a price to estimate how many shares to buy.",
            min_value=0.0,
            format="%.2f",
        ),
        "Live Price": st.column_config.NumberColumn(
            "Live Price ($)",
            disabled=True,
            format="$%.2f",
            help="Current market price fetched live. Use the ↺ Refresh button above to update.",
        ),
    },
    column_order=["Ticker", "Asset Class", "Target Fund?", "Consolidate Into", "Price ($)", "Live Price"],
    num_rows="dynamic",
    hide_index=True,
    use_container_width=True,
    key="ticker_editor",
)
st.session_state.ticker_rows = ticker_df_edited.drop(columns=["Live Price"], errors="ignore").to_dict("records")
mapping = _build_mapping_from_rows(st.session_state.ticker_rows)

# --- Live price fetch ---
# Fetch on first load (prices empty) or when a new ticker appears in the
# mapping, or when the user clicks Refresh.
_price_tickers = [t for t in mapping if not t.startswith("CASH-")]
_should_fetch = (
    _refresh_prices
    or not st.session_state.live_prices
    or bool(set(_price_tickers) - st.session_state.price_tickers)
)
if _should_fetch and _price_tickers:
    with st.sidebar.spinner("Fetching live prices..."):
        _fetched = fetch_prices(_price_tickers)
    if _fetched:
        st.session_state.live_prices = _fetched
        st.session_state.price_tickers = set(_price_tickers)
        st.session_state.price_timestamp = datetime.now().strftime("%H:%M")
        st.rerun()

# --- 5. Rebalance Settings ---
st.sidebar.header("5. Rebalance Settings")
threshold_pct = st.sidebar.number_input(
    "Absolute drift trigger (percentage points)",
    min_value=0.0,
    max_value=50.0,
    value=5.0,
    step=0.5,
    key="threshold_pct",
    help="Rebalance when any asset class moves more than this many percentage points from its target. Example: if your US equity target is 40% and you set this to 5, the tool will recommend trades if US equity falls below 35% or rises above 45%.",
)
threshold_relative_pct = st.sidebar.number_input(
    "Relative drift trigger (%)",
    min_value=0.0,
    max_value=100.0,
    value=20.0,
    step=1.0,
    key="threshold_relative_pct",
    help="Rebalance when drift exceeds this share of the target's own size. Example: if your bond target is 20% and you set this to 20, the tool triggers at below 16% or above 24%. A trade is recommended if EITHER this threshold OR the absolute threshold above is breached — whichever is hit first.",
)
min_trade_value = st.sidebar.number_input(
    "Ignore trades smaller than ($)",
    min_value=0.0,
    value=500.0,
    step=50.0,
    key="min_trade_value",
    help="Any trade smaller than this dollar amount will be left out of the plan. This prevents the tool from recommending trivial buys that aren't worth the effort. Typical values: $100–$500.",
)
whole_shares_only = st.sidebar.checkbox(
    "Whole shares only",
    value=False,
    key="whole_shares_only",
    help="Round all trade quantities down to the nearest whole share. Enable this if your brokerage does not support fractional ETF shares. Note: rounding may leave a small residual cash balance undeployed.",
)

# --- 6. Tax ---
st.sidebar.header("6. Tax")
tax_enabled = st.sidebar.toggle(
    "Tax-smart trading", value=False, key="tax_enabled",
    help="When on, the tool places sells in retirement accounts first (no capital gains tax there), prefers selling losing positions in taxable accounts to offset gains (tax-loss harvesting), and flags trades that could trigger a wash sale rule violation. Recommended for most investors.",
)
uploaded_transactions = st.sidebar.file_uploader(
    "Transaction history CSV (optional)",
    type=["csv"],
    key="transactions_upload",
    help="Upload for wash sale detection. Supports Fidelity's native export or a simplified format (Date, Account, Ticker, Action, Shares).",
)
txn_path = None
if uploaded_transactions:
    _txn_bytes = uploaded_transactions.getvalue()
    _txn_hash = hash(_txn_bytes)
    if _txn_hash != st.session_state.txn_bytes_hash:
        # Content changed (new upload or first upload) — write a fresh temp file.
        st.session_state.txn_bytes_hash = _txn_hash
        st.session_state["_txn_path"] = str(_save_temp(_txn_bytes.decode("utf-8-sig"), ".csv"))
    txn_path = Path(st.session_state["_txn_path"]) if "_txn_path" in st.session_state else None
else:
    # File removed — clear cached path and hash.
    st.session_state.txn_bytes_hash = None
    st.session_state.pop("_txn_path", None)

uploaded_lots = st.sidebar.file_uploader(
    "Tax lot CSV (optional)",
    type=["csv"],
    key="lots_upload",
    help="Upload tax lot data for lot-level sell selection. Format: Account, Ticker, AcquisitionDate, Shares, CostBasisPerShare.",
)
lots_path = None
if uploaded_lots:
    _lots_bytes = uploaded_lots.getvalue()
    _lots_hash = hash(_lots_bytes)
    if _lots_hash != st.session_state.lots_bytes_hash:
        st.session_state.lots_bytes_hash = _lots_hash
        st.session_state["_lots_path"] = str(_save_temp(_lots_bytes.decode("utf-8-sig"), ".csv"))
    lots_path = Path(st.session_state["_lots_path"]) if "_lots_path" in st.session_state else None
else:
    st.session_state.lots_bytes_hash = None
    st.session_state.pop("_lots_path", None)

fidelity_lots_paste = st.sidebar.text_area(
    "Paste Fidelity lot data (optional)",
    value="",
    height=100,
    key="fidelity_lots_paste",
    help="Alternative to uploading a lot CSV — copy-paste the Fidelity Positions page with lots expanded.",
)

# --- 7. External Cash ---
st.sidebar.header("7. External Cash")

st.sidebar.subheader("Investable Cash")
st.sidebar.caption("Cash sitting in a bank account that you're ready to put to work. The tool includes this in your total portfolio and uses it to fund buy trades.")
invest_usd = st.sidebar.number_input(
    "US bank cash ($)",
    min_value=0.0,
    value=0.0,
    step=100.0,
    key="invest_usd",
    help="Cash in a US bank or checking account that you want to invest. Do not enter your Fidelity brokerage cash here — money market positions like SPAXX and FDRXX are already captured in your CSV.",
)
invest_eur = st.sidebar.number_input(
    "European bank cash (\u20ac)",
    min_value=0.0,
    value=0.0,
    step=100.0,
    key="invest_eur",
    help="Cash in a European bank account that you want to invest.",
)

st.sidebar.subheader("Emergency Fund")
st.sidebar.caption("Your emergency fund. Included in your total portfolio value so your allocation picture is accurate, but the tool will never recommend spending it.")
emergency_usd = st.sidebar.number_input(
    "US emergency fund ($)",
    min_value=0.0,
    value=0.0,
    step=100.0,
    key="emergency_usd",
    help="Emergency savings in a US bank. Visible in your portfolio overview but excluded from all rebalancing calculations.",
)
emergency_eur = st.sidebar.number_input(
    "European emergency fund (\u20ac)",
    min_value=0.0,
    value=0.0,
    step=100.0,
    key="emergency_eur",
    help="Emergency savings in a European bank. Visible in your portfolio overview but excluded from all rebalancing calculations.",
)

use_live_fx = st.sidebar.checkbox(
    "Fetch live EUR/USD rate", value=True, key="use_live_fx",
    help="Fetches the current EUR/USD exchange rate automatically. Falls back to the manual rate if unavailable.",
)

_FX_SANITY_MIN = Decimal("0.80")  # EUR/USD historical floor
_FX_SANITY_MAX = Decimal("1.50")  # EUR/USD historical ceiling

# Fetch the live EUR/USD rate at most once per session (or when use_live_fx is
# first enabled).  Caching in session_state prevents a network call on every
# widget interaction — previously `fetch_fx_rate()` fired on every rerun.
if use_live_fx and st.session_state.live_fx_rate is None:
    _fetched_rate = fetch_fx_rate("EUR", "USD")
    if _fetched_rate is not None:
        if _fetched_rate < _FX_SANITY_MIN or _fetched_rate > _FX_SANITY_MAX:
            st.sidebar.warning(
                f"Live EUR/USD rate **{_fetched_rate}** is outside the expected range "
                f"({_FX_SANITY_MIN}–{_FX_SANITY_MAX}). This looks wrong — "
                "falling back to the manual rate below."
            )
        else:
            st.session_state.live_fx_rate = _fetched_rate
            # Pre-fill the manual input with the live rate on first fetch so
            # the user sees the current rate and can override it freely.
            st.session_state.manual_fx = float(_fetched_rate)

if not use_live_fx:
    # User disabled live fetch — clear cached rate so it re-fetches if re-enabled.
    st.session_state.live_fx_rate = None

_live_rate: Decimal | None = st.session_state.live_fx_rate
if use_live_fx:
    if _live_rate is not None:
        st.sidebar.caption(f"Live EUR/USD rate: {_live_rate}")
    else:
        st.sidebar.caption("Could not fetch live rate — using manual rate below.")

# `manual_fx` is initialized in session state above (once, before this widget
# renders).  Omitting `value=` here prevents the input from being reset to the
# live rate on every rerun — the user can freely override it after the initial
# pre-fill without any widget interaction undoing their edit.
manual_fx = st.sidebar.number_input(
    "EUR/USD rate",
    min_value=0.01,
    step=0.01,
    format="%.4f",
    key="manual_fx",
    help="How many US dollars per 1 euro. Example: 1.10 means \u20ac1 = $1.10.",
)

# --- 8. Display Settings ---
show_only_actionable = True
sort_order = [SortKey.SELLS_FIRST, SortKey.LARGEST_TRADE_FIRST]
currency_precision = 0

# --- 9. German Tax (optional expander) ---
with st.sidebar.expander("German Tax (Optional)"):
    german_tax_enabled = st.toggle(
        "Show German tax annotations", value=True, key="german_tax_enabled",
        help="Adds German InvStG analysis to the Trade Plan: Teilfreistellung rates, PFIC risk warnings, and Sparerpauschbetrag reminder.",
    )
    german_tax_filing = "single"
    if german_tax_enabled:
        german_tax_filing = st.selectbox(
            "Filing status",
            options=["single", "married"],
            index=0,
            key="german_tax_filing",
            help="Single filers: \u20ac1,000 Sparerpauschbetrag. Married: \u20ac2,000.",
        )

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


def _build_config(account_mappings: dict[str, AccountType]) -> tuple[RebalanceConfig, list[str]]:
    """Build RebalanceConfig from sidebar widgets and pre-built account mappings."""
    config = RebalanceConfig(
        threshold_pct=Decimal(str(threshold_pct)),
        threshold_relative_pct=Decimal(str(threshold_relative_pct)),
        min_trade_value=Decimal(str(min_trade_value)),
        tlh_enabled=tax_enabled,
        avoid_gains_in_taxable=tax_enabled,
        cash_to_invest=Decimal("0"),
        account_mappings=account_mappings,
        whole_shares_only=whole_shares_only,
    )
    return config, []


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


def _load_all(mapping: dict, account_mappings: dict):
    """Parse all inputs and return (positions, targets, mapping, config, output_config, bank_positions, recent_transactions)."""
    if csv_path is None:
        raise ValueError("No positions CSV provided. Upload a file or check 'Use example CSV'.")

    positions = parse_fidelity_csv(csv_path, account_mappings if account_mappings else None)
    if not positions:
        raise ValueError("No positions found in CSV. Check the file format.")

    # Apply live prices: update each position's price and recalculate market value.
    live = st.session_state.live_prices
    price_warnings: list[str] = []
    if live:
        updated = []
        for p in positions:
            if p.ticker in live and p.quantity > 0:
                # Only update when quantity is known — cash/money-market positions
                # (FDRXX, FCASH, etc.) have quantity=0 in the CSV; their
                # market_value is already the correct dollar balance.
                new_price = live[p.ticker]
                # Sanity check: warn if live price deviates >25% from the CSV price.
                # This catches clearly bad data (e.g. yfinance returning a stale or
                # wrong ticker) before it silently affects trade calculations.
                if p.price and p.price > 0:
                    ratio = float(new_price / p.price)
                    if ratio < 0.50 or ratio > 1.50:
                        # Use \$ to prevent Streamlit from interpreting $ as a LaTeX delimiter.
                        price_warnings.append(
                            f"**{p.ticker}**: live price **\\${float(new_price):.2f}** differs "
                            f"**{abs(1 - ratio):.0%}** from your CSV price **\\${float(p.price):.2f}**. "
                            "This may indicate a wrong ticker or a stock split — verify before trading."
                        )
                new_mv = (new_price * p.quantity).quantize(Decimal("0.01"))
                updated.append(p.model_copy(update={"price": new_price, "market_value": new_mv}))
            else:
                updated.append(p)
        positions = updated
        # Also update mapping prices so preferred-but-not-held tickers
        # get accurate buy estimates.
        for ticker in list(mapping):
            if ticker in live:
                mapping[ticker] = mapping[ticker].model_copy(update={"price": live[ticker]})

    targets = _build_targets()

    config, acct_map_errors = _build_config(account_mappings)

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
    return positions, targets, mapping, config, output_config, bank_positions, recent_transactions, lot_warnings, all_errors, price_warnings


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
            "**Welcome to Portfolio Rebalancer.** This tool looks at what you own today, "
            "compares it to your target mix, and tells you exactly which trades to make — "
            "and in what order.\n\n"
            "**How to use this tool** (work top to bottom in the left panel):\n"
            "- **1. Portfolio Data** — Upload your Fidelity CSV or use the example\n"
            "- **2. Your Accounts** — Match account names to their tax type (Roth, IRA, etc.)\n"
            "- **3. Target Mix** — Set your desired long-term allocation (must sum to 100%)\n"
            "- **4. Fund Classification** — Classify each fund so the tool knows how to handle it. "
            "Add any funds from your CSV that aren't listed\n"
            "- **5. Rebalance Settings** — Adjust how sensitive the rebalance trigger is\n"
            "- **6. Tax** — Enable tax-smart trading and upload lot or transaction data\n"
            "- **7. External Cash** — Add bank cash or emergency fund amounts\n\n"
            "**Getting started:** The example portfolio is pre-loaded. Click the "
            "**Rebalance Analysis** tab to see a sample trade plan — then swap in your "
            "own CSV when ready.\n\n"
            "**Your data never leaves your computer.** The file is read locally in your "
            "browser and is not uploaded anywhere.",
        )
        if st.button("Dismiss", key="dismiss_welcome_btn"):
            st.session_state.dismiss_welcome = True
            st.rerun()

# Tabs
tab_overview, tab_rebalance, tab_trades, tab_consolidation, tab_projection = st.tabs(
    ["1. Your Portfolio", "2. Rebalance Analysis", "3. Trade Plan", "4. Fund Consolidation", "5. After Trades"]
)

# Try to load data
try:
    positions, targets, mapping, config, oc, bank_positions, recent_txns, lot_warnings, lot_errors, price_warnings = _load_all(mapping, account_mappings)
    all_positions = positions + bank_positions
    data_ok = True
except Exception as e:
    data_ok = False
    data_error = str(e)
    lot_warnings = []
    lot_errors = []
    price_warnings = []

if data_ok and lot_errors:
    for err in lot_errors:
        st.error(err)

# ---- Tab 1: Portfolio Overview ----
with tab_overview:
    if not data_ok:
        st.error(f"Cannot load data: {data_error}")
    else:
        if price_warnings:
            for pw in price_warnings:
                st.warning(
                    f"**Live price sanity check:** {pw}",
                    icon="⚠️",
                )

        # Separate investable from emergency — rebalancing only touches investable,
        # so the allocation chart should match exactly what rebalancing operates on.
        investable_positions = [p for p in all_positions if p.ticker not in EMERGENCY_TICKERS]
        emergency_positions  = [p for p in all_positions if p.ticker in EMERGENCY_TICKERS]
        emergency_value = sum(p.market_value for p in emergency_positions)

        total_value, value_by_class, pct_by_class = _compute_allocation(
            investable_positions, mapping, pct_precision=oc.precision.pct
        )
        full_total = total_value + emergency_value  # for the KPI metric

        # KPI row
        col1, col2, col3 = st.columns(3)
        col1.metric("Total Portfolio Value", _format_currency(full_total, cur_prec),
                     help="The combined current market value of every position in your CSV plus any bank cash you entered in the sidebar. Emergency fund is included here but excluded from rebalancing.")
        col2.metric("Accounts", len({p.account_name for p in all_positions}),
                     help="Number of distinct brokerage accounts found in your CSV. Each account has a tax type (Taxable, Roth, IRA, etc.) that affects which accounts get traded first.")
        col3.metric("Positions", len(all_positions),
                     help="Total number of individual holdings across all accounts. Includes cash positions (SPAXX, FDRXX) and any bank cash you entered in the sidebar.")

        st.divider()

        # Charts side by side
        chart_col1, chart_col2 = st.columns(2)

        with chart_col1:
            st.subheader("Current Allocation (what you own today)")
            if emergency_value > 0:
                st.caption(
                    f"Shows your **investable** portfolio ({_format_currency(total_value, cur_prec)}). "
                    f"Emergency fund ({_format_currency(emergency_value, cur_prec)}) is held separately and not rebalanced."
                )
            labels = sorted(pct_by_class.keys())
            values = [_dec(pct_by_class[c]) for c in labels]
            hover = _build_pie_hover_texts(labels, investable_positions, mapping, cur_prec)
            fig = go.Figure(data=[go.Pie(
                labels=labels,
                values=values,
                hole=0.4,
                marker=dict(colors=_pie_colors(labels)),
                customdata=hover,
                hovertemplate="<b>%{label}</b><br>%{value:.1f}% of portfolio<br><br>%{customdata}<extra></extra>",
            )])
            fig.update_layout(margin=dict(t=20, b=20, l=20, r=20), height=350)
            st.plotly_chart(fig, width="stretch")

        with chart_col2:
            st.subheader("Target Allocation (your goal mix)")
            tgt_map = {}
            for t in targets:
                tgt_map[t.asset_class] = _dec(t.target_pct)
            tgt_labels = sorted(tgt_map.keys())
            tgt_values = [tgt_map[c] for c in tgt_labels]
            tgt_hover = _build_target_pie_hover_texts(tgt_labels, mapping)
            fig2 = go.Figure(data=[go.Pie(
                labels=tgt_labels,
                values=tgt_values,
                hole=0.4,
                marker=dict(colors=_pie_colors(tgt_labels)),
                customdata=tgt_hover,
                hovertemplate="<b>%{label}</b><br>Target: %{value:.0f}%<br><br>%{customdata}<extra></extra>",
            )])
            fig2.update_layout(margin=dict(t=20, b=20, l=20, r=20), height=350)
            st.plotly_chart(fig2, width="stretch")

        # Positions table
        st.subheader("Positions")
        pos_rows = []
        for p in all_positions:
            ticker_info = mapping.get(p.ticker)
            asset_class = ticker_info.asset_class if ticker_info else "Not classified — add in sidebar ↑"
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
        rebalance_positions = [p for p in all_positions if p.ticker not in EMERGENCY_TICKERS]
        result = rebalance(rebalance_positions, targets, mapping, config, metadata=metadata, recent_transactions=recent_txns)
        for w in lot_warnings:
            result.warnings.append(w)
        st.session_state.result = result

        if result.warnings:
            for w in result.warnings:
                st.warning(w)

        st.subheader("Allocation vs Target")
        st.caption("Drift shows how far each asset class has moved from your target. A positive bar means you own too much (a candidate to sell). A negative bar means you own too little (a candidate to buy). The dashed orange lines show where a rebalance is triggered.")

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

        # Action Summary
        st.subheader("Action Summary")
        st.caption("This checklist summarizes whether any trades are needed today. A rebalance is recommended the moment any asset class crosses either drift threshold — whichever is hit first.")

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
            st.markdown(f"Rebalancing triggered for: {', '.join(breached)}")
        else:
            st.success("No action needed — all asset classes are within your drift thresholds. Check back after your next contribution or after significant market movement.")

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
                    st.warning(
                        f"Heads up: these taxable sells have an estimated net capital gain of "
                        f"{_format_currency(Decimal(str(net_gain)), cur_prec)}. You may owe taxes "
                        f"on this amount. Review the estimated tax impact below before executing."
                    )

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
                st.subheader("Estimated Capital Gains Tax Impact (Taxable Accounts Only)")
                ti_col1, ti_col2, ti_col3 = st.columns(3)
                ti_col1.metric("Estimated Gains", _format_currency(ti.estimated_total_gains, cur_prec),
                               help="Total estimated profit from sells in taxable accounts, computed as (market value − cost basis) × shares sold. This amount may increase your tax bill for the year. Retirement account sells are excluded — no capital gains tax there.")
                ti_col2.metric("Estimated Losses", _format_currency(ti.estimated_total_losses, cur_prec),
                               help="Total estimated losses from sells in taxable accounts. Losses can offset gains and reduce your tax bill — this is tax-loss harvesting in action. Up to $3,000 of net losses can also offset ordinary income per year.")
                ti_col3.metric(
                    "Net",
                    _format_currency(ti.estimated_net, cur_prec),
                    delta=f"{_format_currency(ti.estimated_net, cur_prec)} taxable" if ti.estimated_net != 0 else None,
                    delta_color="inverse",
                    help="Gains minus losses. A positive number means a net taxable event this year. A negative number means you have harvested more losses than gains — a potential tax benefit.",
                )


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
            if result.warnings:
                for w in result.warnings:
                    st.warning(w)

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
                "Finish every **sell** before placing any **buy** — sells free up the cash that funds the buys. "
                "Steps are grouped by account: complete all trades in one account before moving to the next."
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
                    "Note": "Buys in any of your taxable accounts can draw from this shared balance",
                })
            for acct_name, bal in sorted(pools.tax_adv_pools.items()):
                if bal > 0:
                    pool_rows.append({
                        "Pool": acct_name,
                        "Starting Cash": _format_currency(bal, cur_prec),
                        "Accounts": acct_name,
                        "Note": "This account's cash stays within that account — no cross-account transfers",
                    })
            if pool_rows:
                st.dataframe(pool_rows, width="stretch", hide_index=True)
            else:
                st.caption("No starting cash in any account.")

            # --- Sell phase ---
            if sell_steps:
                st.divider()
                st.markdown(f"### Phase 1: Sells ({len(sell_steps)} trades across {len(sell_accounts)} accounts)")
                st.caption("Sell positions that have grown beyond your target allocation. The cash you raise here is what funds the buys in Phase 2. Proceeds stay within the same account.")

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
                st.caption("Buy into asset classes that are below your target. Each buy uses cash from within the same account — no transfers between accounts are needed.")

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

            # Export section
            st.divider()
            st.subheader("Export Trade Plan")
            _n_shown = len(display_trades)
            _n_hidden = len(result.trades) - _n_shown
            _export_caption = f"Downloads contain the {_n_shown} trade(s) shown above"
            if _n_hidden:
                _export_caption += f" ({_n_hidden} trade(s) below your ${float(config.min_trade_value):,.0f} minimum are excluded)"
            _export_caption += "."
            st.caption(_export_caption)

            from rebalancer.output import write_csv_report, write_markdown_report

            # Build a result containing only the filtered (displayed) trades so the
            # download exactly matches what's shown on screen.
            _export_result = result.model_copy(update={"trades": display_trades})
            _today = datetime.now().strftime("%Y-%m-%d")

            buf_path = _save_temp("", ".md")
            write_markdown_report(_export_result, buf_path, output_config)
            md_content = buf_path.read_text()
            buf_path.unlink(missing_ok=True)  # content is now in memory

            csv_buf_path = _save_temp("", ".csv")
            write_csv_report(_export_result, csv_buf_path, output_config)
            csv_content = csv_buf_path.read_text()
            csv_buf_path.unlink(missing_ok=True)  # content is now in memory

            dl_col1, dl_col2 = st.columns(2)
            with dl_col1:
                st.download_button(
                    label="⬇ Download Markdown",
                    data=md_content,
                    file_name=f"rebalance_{_today}.md",
                    mime="text/markdown",
                    use_container_width=True,
                    help="Human-readable report with allocation table, execution plan, and tax impact summary.",
                )
            with dl_col2:
                st.download_button(
                    label="⬇ Download CSV",
                    data=csv_content,
                    file_name=f"trade_plan_{_today}.csv",
                    mime="text/csv",
                    use_container_width=True,
                    help="Spreadsheet-friendly: one row per trade with account, ticker, shares, value, and gain/loss.",
                )


# ---- Tab 4: Consolidation ----
with tab_consolidation:
    if not data_ok:
        st.error(f"Cannot load data: {data_error}")
    else:
        consolidation = analyze_consolidation(all_positions, mapping)

        # Progress metric
        st.subheader("Portfolio Simplification Progress")
        st.caption("Tracks your progress from multiple funds toward a simplified, lower-maintenance portfolio. The goal is to gradually move everything into your target long-term funds. Cash positions are excluded.")
        col1, col2 = st.columns(2)
        col1.metric(
            "Target Funds",
            _format_currency(consolidation.end_state_value, cur_prec),
            f"{consolidation.end_state_pct}%",
            help="Value held in your preferred long-term funds (marked as 'Target Fund' in Your Funds). These are what you're building toward. The goal is to get this to 100%.",
        )
        col2.metric(
            "Funds to Phase Out",
            _format_currency(consolidation.legacy_value, cur_prec),
            f"{consolidation.legacy_pct}%",
            help="Value still held in older or duplicate funds. These have a consolidation target set in Your Funds. Move them over time as tax-efficient opportunities arise.",
        )

        # Progress bar
        if consolidation.end_state_value + consolidation.legacy_value > 0:
            st.progress(_dec(consolidation.end_state_pct) / 100.0, text=f"{consolidation.end_state_pct}% consolidated")

        if not consolidation.opportunities:
            st.success("Your portfolio is fully consolidated — every position is already in one of your target long-term funds. Nothing to do here.")
        else:
            st.divider()
            st.subheader("Consolidation Opportunities")

            # Separate safe vs wait
            safe_opps = [o for o in consolidation.opportunities if o.safe_to_consolidate]
            wait_opps = [o for o in consolidation.opportunities if not o.safe_to_consolidate]

            if safe_opps:
                st.markdown("#### Ready to Execute — No Tax Cost")
                st.caption("These positions can be consolidated now with no tax cost. Retirement accounts have no capital gains tax on sells. Taxable positions at a loss generate a tax benefit when sold.")
                safe_rows = []
                for opp in safe_opps:
                    gl_str = _format_currency(opp.estimated_gain_loss, cur_prec) if opp.estimated_gain_loss is not None else "-"
                    safe_rows.append({
                        "Ticker": opp.ticker,
                        "Account": opp.account_name,
                        "Value": _format_currency(opp.market_value, cur_prec),
                        "Move Into": opp.consolidate_to,
                        "Gain/Loss": gl_str,
                        "Reason": opp.reason,
                    })
                st.dataframe(safe_rows, width="stretch", hide_index=True)

            if wait_opps:
                st.markdown("#### Hold Off — Taxable Gain")
                st.caption("These positions are at a gain in taxable accounts. Selling now would trigger capital gains tax. Consider waiting for a market dip or a moment when you need to sell anyway.")
                wait_rows = []
                for opp in wait_opps:
                    gl_str = _format_currency(opp.estimated_gain_loss, cur_prec) if opp.estimated_gain_loss is not None else "-"
                    wait_rows.append({
                        "Ticker": opp.ticker,
                        "Account": opp.account_name,
                        "Value": _format_currency(opp.market_value, cur_prec),
                        "Move Into": opp.consolidate_to,
                        "Gain/Loss": gl_str,
                        "Reason": opp.reason,
                    })
                st.dataframe(wait_rows, width="stretch", hide_index=True)


# ---- Tab 5: Post-Trade Projection ----
with tab_projection:
    if not data_ok:
        st.error(f"Cannot load data: {data_error}")
    else:
        result = st.session_state.result
        if result is None:
            st.info("Run the Rebalance Analysis tab first.")
        elif not result.trades:
            st.success("No trades needed — portfolio is already at target.")
        else:
            display_trades = filter_actionable_trades(
                result.trades, config.min_trade_value, show_only_actionable
            )
            projected = project_positions(all_positions, display_trades)
            projected_rebalanceable = [p for p in projected if p.ticker not in EMERGENCY_TICKERS]
            proj_emergency_value = sum(p.market_value for p in projected if p.ticker in EMERGENCY_TICKERS)
            proj_total, proj_by_class, proj_pct_by_class = _compute_allocation(
                projected_rebalanceable, mapping, pct_precision=oc.precision.pct
            )
            proj_full_total = proj_total + proj_emergency_value

            st.subheader("What Your Portfolio Will Look Like After These Trades")
            st.caption("A preview based on the trade plan above. Actual results will vary slightly due to price changes between now and when you execute the trades.")

            col1, col2, col3 = st.columns(3)
            col1.metric(
                "Projected Total Value",
                _format_currency(proj_full_total, cur_prec),
                help="Total portfolio value after trades, including emergency fund. Should be close to current value since rebalancing doesn't add or remove money.",
            )
            col2.metric("Projected Positions", len(projected))
            col3.metric("Trades Applied", len(display_trades))

            st.divider()

            # Side-by-side pie charts: current vs projected
            st.subheader("Allocation Shift")
            chart_col1, chart_col2 = st.columns(2)
            _cur_positions = [p for p in all_positions if p.ticker not in EMERGENCY_TICKERS]
            with chart_col1:
                st.caption("Current")
                cur_labels = sorted(result.current_allocation.keys())
                cur_values = [_dec(result.current_allocation[c]) for c in cur_labels]
                cur_hover = _build_pie_hover_texts(cur_labels, _cur_positions, mapping, cur_prec)
                fig_cur = go.Figure(data=[go.Pie(
                    labels=cur_labels, values=cur_values, hole=0.4,
                    marker=dict(colors=_pie_colors(cur_labels)),
                    customdata=cur_hover,
                    hovertemplate="<b>%{label}</b><br>%{value:.1f}% of portfolio<br><br>%{customdata}<extra></extra>",
                )])
                fig_cur.update_layout(margin=dict(t=10, b=10, l=10, r=10), height=300)
                st.plotly_chart(fig_cur, width="stretch")
            with chart_col2:
                st.caption("Projected")
                proj_labels = sorted(proj_pct_by_class.keys())
                proj_values = [_dec(proj_pct_by_class[c]) for c in proj_labels]
                proj_hover = _build_pie_hover_texts(proj_labels, projected_rebalanceable, mapping, cur_prec)
                fig_proj = go.Figure(data=[go.Pie(
                    labels=proj_labels, values=proj_values, hole=0.4,
                    marker=dict(colors=_pie_colors(proj_labels)),
                    customdata=proj_hover,
                    hovertemplate="<b>%{label}</b><br>%{value:.1f}% of portfolio<br><br>%{customdata}<extra></extra>",
                )])
                fig_proj.update_layout(margin=dict(t=10, b=10, l=10, r=10), height=300)
                st.plotly_chart(fig_proj, width="stretch")

            st.divider()

            # Comparison table: Current / Projected / Target
            st.subheader("Current → Projected → Target")
            all_classes = sorted(
                set(result.current_allocation.keys())
                | set(proj_pct_by_class.keys())
                | set(result.target_allocation.keys())
            )
            proj_rows = []
            for cls in all_classes:
                current_pct = result.current_allocation.get(cls, Decimal("0"))
                projected_pct = proj_pct_by_class.get(cls, Decimal("0"))
                target_pct = result.target_allocation.get(cls, Decimal("0"))
                current_drift = current_pct - target_pct
                projected_drift = projected_pct - target_pct
                proj_rows.append({
                    "Asset Class": cls,
                    "Current %": f"{_dec(current_pct):.2f}",
                    "Projected %": f"{_dec(projected_pct):.2f}",
                    "Target %": f"{_dec(target_pct):.2f}",
                    "Current Drift": f"{_dec(current_drift):+.2f}pp",
                    "Projected Drift": f"{_dec(projected_drift):+.2f}pp",
                })
            st.dataframe(proj_rows, width="stretch", hide_index=True)
            st.caption("After these trades, each asset class should be very close to 0% drift. If you still see significant drift for a class, it may mean there wasn't enough cash to fully rebalance it — consider adding more investable cash in the sidebar.")

            st.divider()

            # Projected positions table
            st.subheader("Projected Positions")
            proj_pos_rows = []
            for p in sorted(projected, key=lambda x: (x.account_name, x.ticker)):
                ticker_info = mapping.get(p.ticker)
                asset_class = ticker_info.asset_class if ticker_info else "unmapped"
                current_pos = next(
                    (cp for cp in all_positions if cp.account_name == p.account_name and cp.ticker == p.ticker),
                    None,
                )
                current_value = current_pos.market_value if current_pos else Decimal("0")
                change = p.market_value - current_value
                proj_pos_rows.append({
                    "Account": p.account_name,
                    "Ticker": p.ticker,
                    "Asset Class": asset_class,
                    "Current Value": _format_currency(current_value, cur_prec) if current_pos else "—",
                    "Projected Value": _format_currency(p.market_value, cur_prec),
                    "Change": _format_currency(change, cur_prec) if current_pos else "(new)",
                    "Projected Shares": f"{_dec(p.quantity):,.3f}",
                })
            st.dataframe(proj_pos_rows, width="stretch", hide_index=True)
