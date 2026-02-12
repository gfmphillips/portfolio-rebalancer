from datetime import date, datetime, timedelta
from decimal import Decimal

from .models import AccountType, Position, TickerMapping, Trade, Transaction

TAX_ADVANTAGED = {
    AccountType.TRADITIONAL_IRA,
    AccountType.ROTH_IRA,
    AccountType.ROTH_401K,
    AccountType.FOUR_01K,
    AccountType.HSA,
}


def find_tlh_opportunities(
    positions: list[Position],
    mapping: dict[str, TickerMapping],
) -> list[tuple[Position, Decimal]]:
    """Find positions in taxable accounts with unrealized losses.

    Returns list of (position, estimated_loss) tuples sorted by largest loss first.
    """
    opportunities: list[tuple[Position, Decimal]] = []

    for p in positions:
        if p.account_type != AccountType.TAXABLE:
            continue
        if p.cost_basis_total is None or p.quantity <= 0:
            continue

        unrealized = p.market_value - p.cost_basis_total
        if unrealized < 0:
            opportunities.append((p, abs(unrealized)))

    # Sort by largest loss first
    opportunities.sort(key=lambda x: x[1], reverse=True)
    return opportunities


def _parse_date(date_str: str) -> date | None:
    """Try to parse a date string in common formats."""
    for fmt in ("%Y-%m-%d", "%m/%d/%Y", "%m-%d-%Y"):
        try:
            return datetime.strptime(date_str.strip(), fmt).date()
        except ValueError:
            continue
    return None


def _recent_buys_from_transactions(
    transactions: list[Transaction],
    mapping: dict[str, TickerMapping],
    days: int = 30,
) -> set[str]:
    """Extract tickers (and their similar tickers) bought within the past N days."""
    cutoff = date.today() - timedelta(days=days)
    recent_tickers: set[str] = set()

    for txn in transactions:
        if txn.action != "BUY":
            continue
        txn_date = _parse_date(txn.date)
        if txn_date is not None and txn_date < cutoff:
            continue
        # If date can't be parsed, include it conservatively
        recent_tickers.add(txn.ticker)
        # Also add similar tickers
        info = mapping.get(txn.ticker)
        if info:
            for sim in info.similar_tickers:
                recent_tickers.add(sim)

    return recent_tickers


def check_wash_sales(
    trades: list[Trade],
    mapping: dict[str, TickerMapping],
    recent_transactions: list[Transaction] | None = None,
) -> list[str]:
    """Check proposed trades for potential wash sale violations.

    A wash sale occurs when you sell a security at a loss and buy the same
    or a "substantially identical" security within 30 days (before or after).

    Since all trades are generated at once, we check if any sell-at-loss in a
    taxable account has a corresponding buy of the same or similar ticker
    in ANY account (including IRAs, 401ks, etc.).

    If recent_transactions is provided, also checks for buys within the past
    30 days that could create wash sale issues with proposed loss-sells.

    Returns a list of warning strings.
    """
    warnings: list[str] = []

    # Find all sells in taxable accounts
    taxable_sells: list[Trade] = [
        t for t in trades
        if t.action == "SELL" and t.account_type == AccountType.TAXABLE
    ]

    # Find all buys across all accounts
    all_buys: list[Trade] = [t for t in trades if t.action == "BUY"]

    # Check if any sells are loss sales (for the incomplete-check warning)
    has_loss_sells = any(
        "TLH" in t.reasoning or any("loss" in w.lower() for w in t.warnings)
        for t in taxable_sells
    )

    if not taxable_sells or (not all_buys and recent_transactions is None):
        # If there are TLH sells but no transaction history, warn about incomplete check
        if has_loss_sells and recent_transactions is None:
            warnings.append(
                "No transaction history provided — wash sale check is INCOMPLETE. "
                "Recent buys (including dividend reinvestments and IRA purchases) "
                "in the past 30 days could create wash sale issues."
            )
        return warnings

    _30_DAY_NOTE = (
        "Note: Wash sale rules apply to purchases 30 days before AND after a "
        "loss sale. Review recent and planned trades outside this tool."
    )

    # Build set of tickers being bought in proposed trades
    buy_tickers: set[str] = set()
    for t in all_buys:
        buy_tickers.add(t.ticker)

    # Build set of tickers recently bought from transaction history
    recent_buy_tickers: set[str] = set()
    if recent_transactions is not None:
        recent_buy_tickers = _recent_buys_from_transactions(
            recent_transactions, mapping
        )

    for sell in taxable_sells:
        # Check if this is a loss sale (indicated by TLH in reasoning)
        is_loss_sale = "TLH" in sell.reasoning or any(
            "loss" in w.lower() for w in sell.warnings
        )
        if not is_loss_sale:
            continue

        sell_ticker = sell.ticker
        sell_info = mapping.get(sell_ticker)

        # Check if same ticker is being bought in proposed trades
        if sell_ticker in buy_tickers:
            buy_accounts = [
                t.account_name for t in all_buys if t.ticker == sell_ticker
            ]
            warnings.append(
                f"WASH SALE RISK: Selling {sell_ticker} at a loss in "
                f"{sell.account_name} while buying {sell_ticker} in "
                f"{', '.join(buy_accounts)}. The loss may be disallowed."
            )

        # Check similar tickers in proposed trades
        if sell_info:
            for similar in sell_info.similar_tickers:
                if similar in buy_tickers:
                    buy_accounts = [
                        t.account_name
                        for t in all_buys
                        if t.ticker == similar
                    ]
                    warnings.append(
                        f"WASH SALE RISK: Selling {sell_ticker} at a loss in "
                        f"{sell.account_name} while buying similar ticker "
                        f"{similar} in {', '.join(buy_accounts)}. "
                        f"The loss may be disallowed."
                    )

        # Also check reverse: is the buy ticker similar to the sell ticker?
        for buy in all_buys:
            buy_info = mapping.get(buy.ticker)
            if buy_info and sell_ticker in buy_info.similar_tickers:
                # Avoid duplicate warnings
                already_warned = any(
                    sell_ticker in w and buy.ticker in w for w in warnings
                )
                if not already_warned:
                    warnings.append(
                        f"WASH SALE RISK: Selling {sell_ticker} at a loss in "
                        f"{sell.account_name} while buying similar ticker "
                        f"{buy.ticker} in {buy.account_name}. "
                        f"The loss may be disallowed."
                    )

        # Check recent transaction history for wash sale risks
        if recent_transactions is not None:
            if sell_ticker in recent_buy_tickers:
                # Find which transactions triggered this
                matching_txns = [
                    txn for txn in recent_transactions
                    if txn.action == "BUY"
                    and (txn.ticker == sell_ticker or
                         (mapping.get(txn.ticker) and sell_ticker in mapping[txn.ticker].similar_tickers) or
                         (sell_info and txn.ticker in sell_info.similar_tickers))
                ]
                if matching_txns:
                    txn_details = [
                        f"{txn.ticker} in {txn.account_name} on {txn.date}"
                        for txn in matching_txns
                    ]
                    # Avoid duplicate if already warned about same ticker from proposed trades
                    already_warned = any(
                        sell_ticker in w and "recent" in w.lower() for w in warnings
                    )
                    if not already_warned:
                        warnings.append(
                            f"WASH SALE RISK (recent history): Selling {sell_ticker} "
                            f"at a loss in {sell.account_name}, but recent buys found: "
                            f"{'; '.join(txn_details)}. "
                            f"The loss may be disallowed."
                        )

    if warnings:
        warnings.append(_30_DAY_NOTE)
    elif has_loss_sells and recent_transactions is None:
        warnings.append(
            "No transaction history provided — wash sale check is INCOMPLETE. "
            "Recent buys (including dividend reinvestments and IRA purchases) "
            "in the past 30 days could create wash sale issues."
        )

    return warnings


def suggest_tlh_replacements(
    ticker: str,
    mapping: dict[str, TickerMapping],
    held_tickers: set[str],
) -> list[str]:
    """Suggest replacement tickers for TLH that won't trigger wash sales.

    Returns similar tickers that are NOT currently held anywhere in the portfolio.
    """
    ticker_info = mapping.get(ticker)
    if not ticker_info:
        return []

    # Find tickers in the same asset class that aren't similar
    same_class_tickers: list[str] = []
    for t, info in mapping.items():
        if t == ticker:
            continue
        if info.asset_class != ticker_info.asset_class:
            continue
        if t in held_tickers:
            continue
        # Check that this ticker isn't "similar" to the sold ticker
        if t not in ticker_info.similar_tickers:
            same_class_tickers.append(t)

    return same_class_tickers
