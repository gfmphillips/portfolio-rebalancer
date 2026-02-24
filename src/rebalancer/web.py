"""Streamlit web GUI for the portfolio rebalancer — policy-aware mode."""

import io
import tempfile
from decimal import Decimal, InvalidOperation
from pathlib import Path

import pandas as pd
import plotly.graph_objects as go
import streamlit as st
import yaml

from rebalancer.basket import basket_template_csv, load_basket_csv
from rebalancer.config import load_mapping
from rebalancer.engine import compute_allocation_views, new_money_plan
from rebalancer.fx import BankCashAccount, build_bank_cash_positions, convert_bank_cash_to_positions, fetch_fx_rate
from rebalancer.models import (
    TAX_ADVANTAGED,
    AccountType,
    AllocationView,
    BasketConstituent,
    BuyPlan,
    DefensiveMode,
    InstrumentType,
    PolicyConfig,
    Position,
    TickerMapping,
    ZERO,
)
from rebalancer.output import format_why_this_plan, write_buy_plan_csv
from rebalancer.parser import parse_fidelity_csv
from rebalancer.persist import (
    apply_ticker_overrides,
    load_settings,
    policy_from_settings,
    save_policy,
    save_ticker_overrides,
    ticker_overrides_from_settings,
)
from rebalancer.theme import inject_theme

# ---------------------------------------------------------------------------
# Page config
# ---------------------------------------------------------------------------
st.set_page_config(
    page_title="Portfolio Rebalancer",
    page_icon="\u2696\ufe0f",
    layout="wide",
)
inject_theme()

# Temp file cleanup
for _stale_path in st.session_state.pop("_temp_paths", []):
    try:
        Path(_stale_path).unlink(missing_ok=True)
    except Exception:
        pass

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------
EXAMPLE_DIR = Path(__file__).parent.parent.parent / "examples"

ASSET_CLASS_COLORS = {
    "cash": "#94a3b8",
    "bonds": "#60a5fa",
    "reit": "#f59e0b",
    "us_equity": "#22c55e",
    "intl_equity": "#8b5cf6",
    "unmapped": "#ef4444",
}
_FALLBACK_COLORS = ["#64748b", "#3b82f6", "#f97316", "#84cc16", "#a855f7", "#ec4899"]

ASSET_CLASS_DISPLAY = {
    "cash": "Cash",
    "bonds": "Bonds",
    "reit": "REIT",
    "us_equity": "US Equity",
    "intl_equity": "Intl Equity",
}
ASSET_CLASS_OPTION_TO_KEY = {v: k for k, v in ASSET_CLASS_DISPLAY.items()}

INSTRUMENT_TYPE_DISPLAY = {t.value: t.value.replace("_", " ").title() for t in InstrumentType}

ACCOUNT_TYPE_LABELS = {
    "taxable": "Taxable (Individual / Brokerage)",
    "traditional_ira": "Traditional IRA / Rollover IRA",
    "roth_ira": "Roth IRA",
    "roth_401k": "Roth 401(k)",
    "401k": "Traditional 401(k)",
    "hsa": "HSA",
}

_SYNTHETIC_TICKER_LABELS = {
    "CASH-USD-INVESTABLE": "US Bank Cash",
    "CASH-EUR-INVESTABLE": "EUR Bank Cash",
    "CASH-USD-EMERGENCY": "US Emergency Fund",
    "CASH-EUR-EMERGENCY": "EUR Emergency Fund",
}

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _dec(val) -> float:
    return float(val)


def _fmt(value: Decimal, precision: int = 0) -> str:
    """Format a Decimal as a dollar string."""
    try:
        if precision == 0:
            return f"${float(value):,.0f}"
        return f"${float(value):,.{precision}f}"
    except Exception:
        return str(value)


def _save_temp(content: str, suffix: str) -> Path:
    tmp = tempfile.NamedTemporaryFile(mode="w", suffix=suffix, delete=False)
    tmp.write(content)
    tmp.flush()
    p = Path(tmp.name)
    st.session_state.setdefault("_temp_paths", []).append(str(p))
    return p


def _pie_colors(labels: list[str]) -> list[str]:
    return [
        ASSET_CLASS_COLORS.get(label, _FALLBACK_COLORS[i % len(_FALLBACK_COLORS)])
        for i, label in enumerate(labels)
    ]


def _allocation_pie(view: AllocationView, title: str) -> go.Figure:
    """Build a stock/defensive allocation pie chart from an AllocationView."""
    labels = ["Stocks", "Defensive"]
    values = [float(view.stock_value), float(view.defensive_value)]
    colors = ["#22c55e", "#60a5fa"]

    fig = go.Figure(go.Pie(
        labels=labels,
        values=values,
        hole=0.5,
        marker_colors=colors,
        textinfo="label+percent",
        hovertemplate="%{label}: %{value:$,.0f}<extra></extra>",
    ))
    fig.update_layout(
        title=title,
        showlegend=False,
        margin=dict(t=40, b=20, l=20, r=20),
        height=280,
    )
    return fig


def _drift_badge(drift: Decimal, band: Decimal) -> str:
    if abs(drift) <= band:
        return "✅ Within bands"
    direction = "overweight ↑" if drift > 0 else "underweight ↓"
    return f"❌ {direction}"


def _load_default_mapping() -> dict[str, TickerMapping]:
    try:
        return load_mapping(EXAMPLE_DIR / "mapping.yaml")
    except Exception:
        return {}


def _parse_csv_bytes(raw_bytes: bytes) -> tuple[list[Position], list[str]]:
    text = raw_bytes.decode("utf-8", errors="replace")
    try:
        positions = parse_fidelity_csv(text)
        return positions, []
    except Exception as exc:
        return [], [str(exc)]


def _apply_account_overrides(
    positions: list[Position],
    account_types: dict[str, str],
) -> list[Position]:
    """Re-label account_type by account_name using user overrides."""
    result = []
    for p in positions:
        override = account_types.get(p.account_name)
        if override:
            try:
                new_type = AccountType(override)
                result.append(p.model_copy(update={"account_type": new_type}))
                continue
            except ValueError:
                pass
        result.append(p)
    return result


# ---------------------------------------------------------------------------
# Session state initialisation
# ---------------------------------------------------------------------------

_RAW_SETTINGS = load_settings()

if "policy" not in st.session_state:
    st.session_state["policy"] = policy_from_settings(_RAW_SETTINGS)
if "ticker_overrides" not in st.session_state:
    st.session_state["ticker_overrides"] = ticker_overrides_from_settings(_RAW_SETTINGS)
if "positions" not in st.session_state:
    st.session_state["positions"] = []
if "mapping" not in st.session_state:
    st.session_state["mapping"] = _load_default_mapping()
if "basket" not in st.session_state:
    st.session_state["basket"] = None
if "buy_plan" not in st.session_state:
    st.session_state["buy_plan"] = None
if "account_type_overrides" not in st.session_state:
    st.session_state["account_type_overrides"] = {}
if "csv_bytes_hash" not in st.session_state:
    st.session_state["csv_bytes_hash"] = None

policy: PolicyConfig = st.session_state["policy"]

# ---------------------------------------------------------------------------
# Sidebar
# ---------------------------------------------------------------------------

with st.sidebar:
    st.header("📂 Portfolio Upload")

    uploaded = st.file_uploader(
        "Fidelity Positions CSV",
        type=["csv"],
        help="Export from Fidelity: Accounts → Positions → Download",
    )
    if uploaded is not None:
        raw_bytes = uploaded.read()
        h = hash(raw_bytes)
        if h != st.session_state["csv_bytes_hash"]:
            st.session_state["csv_bytes_hash"] = h
            parsed, errs = _parse_csv_bytes(raw_bytes)
            if errs:
                st.error(f"Parse error: {errs[0]}")
            else:
                st.session_state["positions"] = parsed
                st.session_state["buy_plan"] = None  # invalidate cached plan

    # Bank cash inputs
    st.markdown("---")
    st.subheader("🏦 Bank Cash")
    col1, col2 = st.columns(2)
    with col1:
        inv_eur = st.number_input(
            "Investable cash (€)",
            min_value=0.0,
            value=float(policy.investable_cash_eur),
            step=1000.0,
            format="%.0f",
            key="sb_investable_eur",
        )
    with col2:
        monthly_eur = st.number_input(
            "Monthly savings (€)",
            min_value=0.0,
            value=float(policy.monthly_investable_cash_eur),
            step=100.0,
            format="%.0f",
            key="sb_monthly_eur",
        )

    fx_val = st.number_input(
        "EUR/USD rate",
        min_value=0.80,
        max_value=2.00,
        value=float(policy.eurusd_fx),
        step=0.01,
        format="%.2f",
        key="sb_eurusd",
    )

    # Update policy in-place if cash inputs changed
    new_inv = Decimal(str(inv_eur))
    new_monthly = Decimal(str(monthly_eur))
    new_fx = Decimal(str(round(fx_val, 4)))
    if (
        policy.investable_cash_eur != new_inv
        or policy.monthly_investable_cash_eur != new_monthly
        or policy.eurusd_fx != new_fx
    ):
        st.session_state["policy"] = PolicyConfig(
            **{
                **policy.__dict__,
                "investable_cash_eur": new_inv,
                "monthly_investable_cash_eur": new_monthly,
                "eurusd_fx": new_fx,
            }
        )
        policy = st.session_state["policy"]
        st.session_state["buy_plan"] = None

    # Bank EUR cash as investable position
    if inv_eur > 0:
        bank_pos = BankCashAccount(
            account_name="Bank EUR",
            eur_amount=new_inv,
            is_investable=True,
        )
        st.session_state.setdefault("bank_positions", [])
        bank_positions = convert_bank_cash_to_positions([bank_pos], float(new_fx))
        # Inject into positions if not already there
        base = [p for p in st.session_state["positions"] if not p.ticker.startswith("CASH-EUR")]
        st.session_state["positions"] = base + bank_positions

    # Positions summary
    if st.session_state["positions"]:
        total = sum(p.market_value for p in st.session_state["positions"])
        n = len(st.session_state["positions"])
        st.caption(f"Loaded {n} positions · total {_fmt(total)}")

    # Compute plan button
    st.markdown("---")
    if st.button("⚡ Compute Buy Plan", type="primary", use_container_width=True):
        positions = st.session_state["positions"]
        mapping = st.session_state["mapping"]
        p = st.session_state["policy"]
        basket = st.session_state["basket"]

        # Apply ticker overrides to mapping
        mapping = apply_ticker_overrides(mapping, st.session_state["ticker_overrides"])

        with st.spinner("Computing plan…"):
            try:
                plan = new_money_plan(positions, p, mapping, basket=basket)
                st.session_state["buy_plan"] = plan
            except Exception as exc:
                st.error(f"Plan error: {exc}")
                st.session_state["buy_plan"] = None

# ---------------------------------------------------------------------------
# Banner
# ---------------------------------------------------------------------------

st.warning(
    "**Policy:** Legacy ETFs are hold/sell-only. New purchases are limited to individual "
    "stocks (via basket) and Treasury/CD placeholders. Selling legacy ETFs is irreversible — "
    "no new ETF purchases are possible in any account.",
    icon="⚠️",
)

# ---------------------------------------------------------------------------
# Main tabs
# ---------------------------------------------------------------------------

tab1, tab2, tab3, tab4 = st.tabs([
    "📊 Overview",
    "⚙️ Settings",
    "💸 Buy Plan",
    "🗂️ Holdings & Classification",
])

# ============================================================================
# Tab 1 — Overview
# ============================================================================

with tab1:
    positions: list[Position] = st.session_state["positions"]
    mapping: dict[str, TickerMapping] = apply_ticker_overrides(
        st.session_state["mapping"], st.session_state["ticker_overrides"]
    )
    p: PolicyConfig = st.session_state["policy"]

    if not positions:
        st.info("Upload a Fidelity positions CSV in the sidebar to get started.", icon="📂")
        st.stop()

    total_view, impl_view = compute_allocation_views(positions, p, mapping)

    # Band status
    band_pct = float(p.rebalance_band_abs * 100)
    target_pct = float(p.target_stock_pct * 100)
    stock_pct = float(total_view.stock_pct * 100)
    drift_pct = float(total_view.stock_drift * 100)

    if total_view.within_bands:
        st.success(
            f"Portfolio is **within bands** — stocks: {stock_pct:.1f}% "
            f"(target {target_pct:.1f}% ±{band_pct:.1f}%)",
            icon="✅",
        )
    else:
        direction = "overweight" if total_view.stock_drift > 0 else "underweight"
        st.warning(
            f"Portfolio is **outside bands** — stocks: {stock_pct:.1f}% "
            f"(target {target_pct:.1f}% ±{band_pct:.1f}%, {direction} by {abs(drift_pct):.1f} ppts)",
            icon="❌",
        )

    # Horizon warning
    plan_cached: BuyPlan | None = st.session_state.get("buy_plan")
    if plan_cached and plan_cached.months_to_reenter_band is not None:
        m = int(plan_cached.months_to_reenter_band)
        if m > p.horizon_months:
            st.error(
                f"⏳ At €{float(p.monthly_investable_cash_eur):,.0f}/month, it takes ≈**{m} months** "
                f"to re-enter the target band — exceeds your {p.horizon_months}-month horizon. "
                "Consider enabling **allow_legacy_etf_sales** in Settings.",
                icon="⏳",
            )

    # Side-by-side: Total vs Implementable
    col_total, col_impl = st.columns(2)

    with col_total:
        st.markdown("### Total Portfolio")
        if total_view.total_value > ZERO:
            st.plotly_chart(_allocation_pie(total_view, ""), use_container_width=True)
        drift_df = pd.DataFrame([
            {
                "Metric": "Stocks",
                "Current": f"{float(total_view.stock_pct * 100):.1f}%",
                "Target": f"{float(p.target_stock_pct * 100):.1f}%",
                "Drift": f"{float(total_view.stock_drift * 100):+.1f} ppts",
            },
            {
                "Metric": "Defensive",
                "Current": f"{float(total_view.bond_pct * 100):.1f}%",
                "Target": f"{float(p.target_bond_pct * 100):.1f}%",
                "Drift": f"{float((total_view.bond_pct - p.target_bond_pct) * 100):+.1f} ppts",
            },
        ])
        st.dataframe(drift_df, use_container_width=True, hide_index=True)
        st.caption(f"Total value: **{_fmt(total_view.total_value)}**")

    with col_impl:
        st.markdown("### Implementable (Buy-Enabled Accounts)")
        if impl_view.total_value > ZERO:
            st.plotly_chart(_allocation_pie(impl_view, ""), use_container_width=True)
        drift_df2 = pd.DataFrame([
            {
                "Metric": "Stocks",
                "Current": f"{float(impl_view.stock_pct * 100):.1f}%",
                "Target": f"{float(p.target_stock_pct * 100):.1f}%",
                "Drift": f"{float(impl_view.stock_drift * 100):+.1f} ppts",
            },
            {
                "Metric": "Defensive",
                "Current": f"{float(impl_view.bond_pct * 100):.1f}%",
                "Target": f"{float(p.target_bond_pct * 100):.1f}%",
                "Drift": f"{float((impl_view.bond_pct - p.target_bond_pct) * 100):+.1f} ppts",
            },
        ])
        st.dataframe(drift_df2, use_container_width=True, hide_index=True)
        if impl_view.excluded_value > ZERO:
            st.caption(
                f"Implementable value: **{_fmt(impl_view.total_value)}** "
                f"(+{_fmt(impl_view.excluded_value)} excluded: {impl_view.excluded_reason})"
            )
        else:
            st.caption(f"Implementable value: **{_fmt(impl_view.total_value)}**")

    # Positions table
    st.markdown("---")
    st.markdown("### All Positions")
    rows = []
    for pos in sorted(positions, key=lambda p: p.account_name):
        tm = mapping.get(pos.ticker)
        rows.append({
            "Account": pos.account_name,
            "Type": pos.account_type.value,
            "Ticker": pos.ticker,
            "Asset Class": tm.asset_class if tm else "unmapped",
            "Instrument": (tm.instrument_type.value if tm else "—"),
            "Value": _fmt(pos.market_value),
            "Qty": float(pos.quantity),
            "Price": _fmt(pos.price, 2),
        })
    if rows:
        st.dataframe(pd.DataFrame(rows), use_container_width=True, hide_index=True)

    # Unclassified tickers warning
    unclassified = sorted({
        p.ticker for p in positions
        if not mapping.get(p.ticker) and not p.ticker.startswith("CASH-")
    })
    if unclassified:
        st.warning(
            f"**{len(unclassified)} unclassified ticker(s):** {', '.join(unclassified)} — "
            "classify them in the **Holdings & Classification** tab.",
            icon="⚠️",
        )


# ============================================================================
# Tab 2 — Settings
# ============================================================================

with tab2:
    st.markdown("### Policy Settings")
    st.caption("Changes are saved automatically to ~/.portfolio-rebalancer/settings.json")

    p: PolicyConfig = st.session_state["policy"]

    with st.expander("🎯 Allocation Targets", expanded=True):
        col1, col2 = st.columns(2)
        with col1:
            tgt_stock = st.number_input(
                "Target stock %",
                0.0, 100.0,
                value=float(p.target_stock_pct * 100),
                step=1.0,
            )
            band = st.number_input(
                "Rebalance band (ppts)",
                0.0, 20.0,
                value=float(p.rebalance_band_abs * 100),
                step=0.5,
            )
        with col2:
            tgt_bond = st.number_input(
                "Target defensive %",
                0.0, 100.0,
                value=float(p.target_bond_pct * 100),
                step=1.0,
            )
            horizon = st.number_input("Horizon (months)", 1, 120, int(p.horizon_months))

    with st.expander("🏦 Defensive Allocation Mode", expanded=False):
        def_mode_opts = {
            DefensiveMode.treasury_only: "Treasury only (single TREASURY row)",
            DefensiveMode.treasury_cd_split: "Treasury + CD split",
            DefensiveMode.ladder: "Bond ladder (multiple maturities)",
        }
        mode_label = st.radio(
            "Defensive mode",
            list(def_mode_opts.values()),
            index=list(def_mode_opts.keys()).index(p.defensive_mode),
        )
        chosen_mode = [k for k, v in def_mode_opts.items() if v == mode_label][0]

        t_pct, c_pct = p.treasury_pct, p.cd_pct
        ladder_rungs, ladder_cur = p.ladder_rungs_months, p.ladder_currency

        if chosen_mode == DefensiveMode.treasury_cd_split:
            col1, col2 = st.columns(2)
            with col1:
                t_pct = Decimal(str(st.number_input("Treasury %", 0.0, 100.0,
                    value=float(p.treasury_pct * 100), step=5.0) / 100))
            with col2:
                c_pct = Decimal(str(st.number_input("CD %", 0.0, 100.0,
                    value=float(p.cd_pct * 100), step=5.0) / 100))
        elif chosen_mode == DefensiveMode.ladder:
            ladder_rungs_raw = st.text_input(
                "Ladder rungs (months, comma-separated)",
                value=",".join(str(r) for r in p.ladder_rungs_months),
            )
            try:
                ladder_rungs = [int(x.strip()) for x in ladder_rungs_raw.split(",") if x.strip()]
            except ValueError:
                st.error("Enter comma-separated integers, e.g. 6,12,24,36")
            ladder_cur = st.selectbox("Ladder currency", ["EUR", "USD"],
                index=0 if p.ladder_currency == "EUR" else 1)

    with st.expander("🔐 Account Routing", expanded=False):
        all_acct_types = list(ACCOUNT_TYPE_LABELS.items())
        enabled = st.multiselect(
            "Buy-enabled account types",
            options=[v for _, v in all_acct_types],
            default=[ACCOUNT_TYPE_LABELS.get(k, k) for k in p.buy_enabled_account_types],
        )
        buy_enabled = frozenset(
            k for k, v in all_acct_types if v in enabled
        )

    with st.expander("🛡️ Advanced / Feature Flags", expanded=False):
        allow_sells = st.checkbox(
            "allow_legacy_etf_sales — enable ETF sell recommendations",
            value=p.allow_legacy_etf_sales,
        )
        min_trade = st.number_input(
            "Minimum trade value ($)",
            0.0, 10000.0,
            value=float(p.min_trade_value),
            step=10.0,
        )
        basket_size = st.number_input(
            "Basket size (max # stocks)",
            1, 500,
            int(p.basket_size),
        )

    with st.expander("🧺 Basket CSV", expanded=False):
        # Template download
        tmpl = basket_template_csv()
        st.download_button(
            "⬇ Download CSV template",
            data=tmpl,
            file_name="basket_template.csv",
            mime="text/csv",
        )

        basket_file = st.file_uploader("Upload basket CSV", type=["csv"])
        basket_paste = st.text_area(
            "…or paste CSV here",
            height=120,
            placeholder="ticker,target_weight\nAAPL,7.00\nMSFT,5.50\n…",
        )

        basket_version = None
        basket_loaded = None

        if basket_file is not None:
            try:
                basket_text = basket_file.read().decode("utf-8", errors="replace")
                basket_loaded = load_basket_csv(csv_text=basket_text)
                # Extract version from filename e.g. basket_us_equity_v2024-03-01.csv
                import re
                m = re.search(r"v(\d{4}-\d{2}-\d{2})", basket_file.name)
                basket_version = m.group(1) if m else None
                st.success(f"Loaded {len(basket_loaded)} basket constituents" +
                           (f" · version {basket_version}" if basket_version else ""))
            except Exception as exc:
                st.error(f"Basket CSV error: {exc}")
        elif basket_paste and basket_paste.strip():
            try:
                basket_loaded = load_basket_csv(csv_text=basket_paste)
                st.success(f"Loaded {len(basket_loaded)} basket constituents from paste")
            except Exception as exc:
                st.error(f"Basket CSV error: {exc}")

        if basket_loaded is not None:
            st.session_state["basket"] = basket_loaded

    if st.button("💾 Save Settings", type="primary"):
        new_policy = PolicyConfig(
            target_stock_pct=Decimal(str(tgt_stock / 100)),
            target_bond_pct=Decimal(str(tgt_bond / 100)),
            target_us_equity_pct_of_equity=p.target_us_equity_pct_of_equity,
            bank_cash_target_eur=p.bank_cash_target_eur,
            investable_cash_eur=p.investable_cash_eur,
            monthly_investable_cash_eur=p.monthly_investable_cash_eur,
            eurusd_fx=p.eurusd_fx,
            rebalance_band_abs=Decimal(str(band / 100)),
            horizon_months=int(horizon),
            basket_size=int(basket_size),
            min_trade_value=Decimal(str(min_trade)),
            allow_international_basket=p.allow_international_basket,
            allow_legacy_etf_sales=allow_sells,
            buy_enabled_account_types=buy_enabled,
            defensive_mode=chosen_mode,
            treasury_pct=t_pct,
            cd_pct=c_pct,
            ladder_rungs_months=ladder_rungs,
            ladder_currency=ladder_cur,
            basket_csv_path=p.basket_csv_path,
            basket_version=basket_version or p.basket_version,
        )
        st.session_state["policy"] = new_policy
        st.session_state["buy_plan"] = None
        raw = load_settings()
        save_policy(new_policy, raw)
        st.success("Settings saved!")

    if st.session_state.get("basket") and st.session_state["basket"]:
        basket_list: list[BasketConstituent] = st.session_state["basket"]
        st.markdown("#### Basket preview")
        basket_df = pd.DataFrame([
            {
                "Ticker": c.ticker,
                "Weight": f"{float(c.target_weight * 100):.2f}%",
                "Name": c.name,
                "Sector": c.sector,
                "ADR": "Yes" if c.is_adr else "",
            }
            for c in basket_list
        ])
        st.dataframe(basket_df, use_container_width=True, hide_index=True)


# ============================================================================
# Tab 3 — Buy Plan
# ============================================================================

with tab3:
    plan: BuyPlan | None = st.session_state.get("buy_plan")
    positions_t3: list[Position] = st.session_state["positions"]
    p_t3: PolicyConfig = st.session_state["policy"]

    if not positions_t3:
        st.info("Upload a CSV in the sidebar first.", icon="📂")
    elif plan is None:
        st.info("Click **⚡ Compute Buy Plan** in the sidebar to generate a plan.", icon="💡")
    else:
        # Warnings
        if plan.warnings:
            for w in plan.warnings:
                st.warning(w, icon="⚠️")

        # Why this plan
        with st.expander("📖 Why this plan", expanded=True):
            st.write(plan.why_text)

        # Cash summary
        col1, col2, col3 = st.columns(3)
        col1.metric("Investable cash", _fmt(plan.investable_cash_usd))
        col2.metric("→ Equity", _fmt(plan.equity_cash_usd))
        col3.metric("→ Defensive", _fmt(plan.defensive_cash_usd))

        # Equity instructions
        if plan.equity_instructions:
            st.markdown("### Equity Buy Instructions")
            eq_rows = []
            for t in plan.equity_instructions:
                eq_rows.append({
                    "Ticker": t.ticker,
                    "Shares": str(t.shares) if t.shares > ZERO else "—",
                    "Est. Value": _fmt(t.estimated_value),
                    "Account": t.account_name,
                    "Reasoning": t.reasoning,
                })
            st.dataframe(pd.DataFrame(eq_rows), use_container_width=True, hide_index=True)

        # Defensive instructions
        if plan.defensive_instructions:
            st.markdown("### Defensive Placeholder Instructions")
            st.caption(
                "These are manual actions — place through your broker. "
                "Ticker names are placeholders (TREASURY, CD, etc.)."
            )
            def_rows = []
            for t in plan.defensive_instructions:
                def_rows.append({
                    "Placeholder": t.ticker,
                    "Amount": _fmt(t.estimated_value),
                    "Account": t.account_name,
                    "Action": "Execute manually",
                })
            st.dataframe(pd.DataFrame(def_rows), use_container_width=True, hide_index=True)

        # Download CSV
        st.markdown("---")
        buf = io.StringIO()
        import csv as _csv, datetime as _dt
        writer = _csv.writer(buf)
        writer.writerow(["# generated_at", _dt.datetime.now().strftime("%Y-%m-%d %H:%M")])
        writer.writerow(["# total_portfolio",
                          f"${float(plan.total_view.total_value):,.0f}",
                          f"stocks: {float(plan.total_view.stock_pct * 100):.1f}%"])
        writer.writerow(["side", "action", "ticker", "shares", "est_value_usd",
                          "account", "account_type", "reasoning"])
        for t in plan.equity_instructions:
            writer.writerow(["equity", t.action, t.ticker,
                              str(t.shares) if t.shares > ZERO else "",
                              f"${float(t.estimated_value):,.2f}",
                              t.account_name, t.account_type.value, t.reasoning])
        for t in plan.defensive_instructions:
            writer.writerow(["defensive", t.action, t.ticker, "",
                              f"${float(t.estimated_value):,.2f}",
                              t.account_name, t.account_type.value, t.reasoning])

        st.download_button(
            "⬇ Download Buy Plan CSV",
            data=buf.getvalue(),
            file_name="buy_plan.csv",
            mime="text/csv",
        )

        # Legacy sell flags
        if plan.legacy_sell_flags:
            st.markdown("---")
            st.markdown("### Legacy ETF Sell Flags")
            for flag in plan.legacy_sell_flags:
                st.info(flag, icon="🏳️")

        if plan.legacy_sell_trades:
            st.markdown("### Legacy ETF Sell Recommendations")
            st.warning(
                "**Selling legacy ETFs is irreversible.** "
                "These positions cannot be re-purchased.",
                icon="⚠️",
            )
            sell_rows = [{
                "Ticker": t.ticker,
                "Account": t.account_name,
                "Value": _fmt(t.estimated_value),
                "Reasoning": t.reasoning,
            } for t in plan.legacy_sell_trades]
            st.dataframe(pd.DataFrame(sell_rows), use_container_width=True, hide_index=True)


# ============================================================================
# Tab 4 — Holdings & Classification
# ============================================================================

with tab4:
    positions_t4: list[Position] = st.session_state["positions"]
    mapping_t4: dict[str, TickerMapping] = st.session_state["mapping"]
    overrides_t4: dict[str, dict] = st.session_state["ticker_overrides"]

    st.markdown("### Holdings & Classification")
    st.caption(
        "Set **instrument_type** to control buy-eligibility. "
        "Set **never_want** to flag positions for eventual exit. "
        "Changes are applied immediately to the next plan computation."
    )

    # Build editable rows from current positions + existing mapping
    all_tickers = sorted({
        p.ticker for p in positions_t4
        if not p.ticker.startswith("CASH-")
    })

    if not all_tickers:
        st.info("Upload a CSV to see positions here.", icon="📂")
    else:
        instrument_options = [t.value for t in InstrumentType]
        asset_class_options = ["cash", "bonds", "reit", "us_equity", "intl_equity"]

        rows = []
        for ticker in all_tickers:
            tm = mapping_t4.get(ticker)
            ov = overrides_t4.get(ticker, {})
            rows.append({
                "Ticker": ticker,
                "Asset Class": tm.asset_class if tm else "us_equity",
                "Instrument Type": ov.get(
                    "instrument_type",
                    tm.instrument_type.value if tm else InstrumentType.legacy_fund_or_etf.value,
                ),
                "Never Want": bool(ov.get("never_want", tm.never_want if tm else False)),
                "Account Type": next(
                    (p.account_type.value for p in positions_t4 if p.ticker == ticker),
                    "taxable",
                ),
            })

        edited = st.data_editor(
            pd.DataFrame(rows),
            use_container_width=True,
            hide_index=True,
            column_config={
                "Ticker": st.column_config.TextColumn("Ticker", disabled=True),
                "Asset Class": st.column_config.SelectboxColumn(
                    "Asset Class",
                    options=asset_class_options,
                ),
                "Instrument Type": st.column_config.SelectboxColumn(
                    "Instrument Type",
                    options=instrument_options,
                    help="Controls buy eligibility. legacy_fund_or_etf = sell-only (default).",
                ),
                "Never Want": st.column_config.CheckboxColumn(
                    "Never Want",
                    help="Flag for eventual exit when allow_legacy_etf_sales is enabled.",
                ),
                "Account Type": st.column_config.TextColumn(
                    "Account Type",
                    disabled=True,
                ),
            },
            key="holdings_editor",
        )

        if st.button("💾 Save Classification Overrides"):
            new_overrides = {}
            new_mapping = {}

            for _, row in edited.iterrows():
                ticker = row["Ticker"]
                it_val = row["Instrument Type"]
                never_want = bool(row["Never Want"])
                asset_class = row["Asset Class"]

                new_overrides[ticker] = {
                    "instrument_type": it_val,
                    "never_want": never_want,
                }

                # Update the in-memory mapping
                tm = mapping_t4.get(ticker)
                try:
                    it = InstrumentType(it_val)
                except ValueError:
                    it = InstrumentType.legacy_fund_or_etf

                if tm:
                    new_mapping[ticker] = tm.model_copy(
                        update={"instrument_type": it, "never_want": never_want,
                                "asset_class": asset_class}
                    )
                else:
                    new_mapping[ticker] = TickerMapping(
                        asset_class=asset_class,
                        instrument_type=it,
                        never_want=never_want,
                    )

            # Merge with existing mapping (non-edited tickers preserved)
            merged_mapping = {**mapping_t4, **new_mapping}
            st.session_state["mapping"] = merged_mapping
            st.session_state["ticker_overrides"] = new_overrides
            st.session_state["buy_plan"] = None  # invalidate

            raw = load_settings()
            save_ticker_overrides(new_overrides, raw)
            st.success("Classification overrides saved!")
