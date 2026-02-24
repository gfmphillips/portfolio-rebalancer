"""Policy enforcement for the new-money-only investing strategy.

All functions here are pure (no I/O, no Streamlit).  They answer:
  - Is this instrument type blocked from new buys?
  - Is this account buy-enabled under current policy?
  - How many months until the portfolio re-enters the target band?
  - Should we flag / recommend selling a legacy ETF position?
"""

from decimal import ROUND_CEILING, Decimal

from .models import (
    BLOCKED_BUY_TYPES,
    AccountType,
    InstrumentType,
    PolicyConfig,
    Position,
    TAX_ADVANTAGED,
    TickerMapping,
    Trade,
    ZERO,
)

_ONE = Decimal("1")


def is_etf_buy_blocked(instrument_type: InstrumentType) -> bool:
    """Return True if new purchases of this instrument type are prohibited."""
    return instrument_type in BLOCKED_BUY_TYPES


def is_account_buy_enabled(acct_type: AccountType, policy: PolicyConfig) -> bool:
    """Return True if new money can be deployed into this account type."""
    return acct_type.value in policy.buy_enabled_account_types


def months_to_reenter_band(
    stock_value_usd: Decimal,
    total_value_usd: Decimal,
    target_stock_pct: Decimal,
    band_abs: Decimal,
    monthly_new_cash_usd: Decimal,
) -> Decimal | None:
    """Estimate how many months of new cash it takes to re-enter the target band.

    Uses pure algebra — no return assumptions.  Returns None if monthly_new_cash == 0.

    Overweight case (stock_pct > target + band):
        Each month, all new cash goes to defensive; stock value unchanged.
        Solve: stock_value / (total + n * monthly) = upper_band
        n = (stock_value - total * upper_band) / (monthly * upper_band)

    Underweight case (stock_pct < target - band):
        Each month, all new cash goes to equity; both stock and total grow.
        Solve: (stock_value + n * monthly) / (total + n * monthly) = lower_band
        n = (lower_band * total - stock_value) / (monthly * (1 - lower_band))

    Returns the ceiling as a Decimal so callers can compare to horizon_months.
    """
    if monthly_new_cash_usd <= ZERO or total_value_usd <= ZERO:
        return None

    current_pct = stock_value_usd / total_value_usd
    upper_band = target_stock_pct + band_abs
    lower_band = target_stock_pct - band_abs

    if current_pct > upper_band:
        # Overweight: new cash → defensive
        numerator = stock_value_usd - total_value_usd * upper_band
        denominator = monthly_new_cash_usd * upper_band
        if denominator <= ZERO:
            return None
        n = numerator / denominator
    elif current_pct < lower_band:
        # Underweight: new cash → equity
        numerator = lower_band * total_value_usd - stock_value_usd
        denominator = monthly_new_cash_usd * (_ONE - lower_band)
        if denominator <= ZERO:
            return None
        n = numerator / denominator
    else:
        return Decimal("0")  # already within band

    if n <= ZERO:
        return Decimal("0")

    return n.to_integral_value(rounding=ROUND_CEILING)


def should_recommend_legacy_sell(
    position: Position,
    mapping: dict[str, TickerMapping],
    policy: PolicyConfig,
    outside_band: bool,
    months_to_fix: Decimal | None,
) -> tuple[bool, str]:
    """Decide whether to recommend/flag selling a legacy ETF position.

    Returns (recommend: bool, reason: str).

    Conditions for True:
      allow_legacy_etf_sales=True AND (
          never_want=True
          OR (outside_band AND months_to_fix is not None AND months_to_fix > horizon)
      )

    Positions in buy-enabled accounts that are not ETFs are never flagged here
    (they can be freely traded without special permission).
    """
    tm = mapping.get(position.ticker)
    if tm is None:
        return False, ""

    # Non-ETF positions in buy-enabled accounts need no special flag
    if (
        is_account_buy_enabled(position.account_type, policy)
        and tm.instrument_type not in BLOCKED_BUY_TYPES
    ):
        return False, ""

    if not policy.allow_legacy_etf_sales:
        return False, ""

    if tm.never_want:
        return True, f"{position.ticker}: marked never_want"

    if (
        outside_band
        and months_to_fix is not None
        and months_to_fix > policy.horizon_months
    ):
        return True, (
            f"{position.ticker}: portfolio outside band and "
            f"≈{int(months_to_fix)} months to correct (>{policy.horizon_months}-month horizon)"
        )

    return False, ""


def build_legacy_sell_flags(
    positions: list[Position],
    mapping: dict[str, TickerMapping],
    policy: PolicyConfig,
    outside_band: bool,
    months_to_fix: Decimal | None,
) -> tuple[list[str], list[Trade]]:
    """Build advisory sell flags and (if enabled) actual sell Trade objects.

    Returns (advisory_flags, sell_trades).
    advisory_flags is always built regardless of allow_legacy_etf_sales.
    sell_trades is non-empty only when allow_legacy_etf_sales=True.
    """
    # Compute advisory flags even when allow_legacy_etf_sales=False so the UI
    # can surface "you could sell X but the feature is disabled" messages.
    flag_reasons: list[str] = []
    sell_trades: list[Trade] = []

    # We need to know when a position *would* qualify for a sell recommendation
    # even if the feature is off, so we temporarily check with a cloned policy.
    for pos in positions:
        tm = mapping.get(pos.ticker)
        if tm is None:
            continue
        if tm.instrument_type not in BLOCKED_BUY_TYPES:
            continue  # only flag ETF/fund positions

        recommend, reason = should_recommend_legacy_sell(
            pos, mapping, policy, outside_band, months_to_fix
        )
        # Build advisory flag regardless of allow_legacy_etf_sales
        # (check the underlying condition directly)
        if not policy.allow_legacy_etf_sales:
            # Compute advisory flag using same logic but ignoring the gate
            if tm.never_want:
                flag_reasons.append(
                    f"{pos.ticker}: marked never_want — "
                    "enable allow_legacy_etf_sales to act on this"
                )
            elif (
                outside_band
                and months_to_fix is not None
                and months_to_fix > policy.horizon_months
            ):
                flag_reasons.append(
                    f"{pos.ticker}: ≈{int(months_to_fix)} months to correct — "
                    "enable allow_legacy_etf_sales to act on this"
                )
        else:
            if recommend:
                flag_reasons.append(reason)
                sell_trades.append(
                    Trade(
                        account_name=pos.account_name,
                        account_type=pos.account_type,
                        ticker=pos.ticker,
                        action="SELL",
                        shares=pos.quantity,
                        estimated_value=pos.market_value,
                        reasoning=reason,
                    )
                )

    return flag_reasons, sell_trades
