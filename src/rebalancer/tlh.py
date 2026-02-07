from decimal import Decimal

from .models import AccountType, Position, TickerMapping, Trade

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


def check_wash_sales(
    trades: list[Trade],
    mapping: dict[str, TickerMapping],
) -> list[str]:
    """Check proposed trades for potential wash sale violations.

    A wash sale occurs when you sell a security at a loss and buy the same
    or a "substantially identical" security within 30 days (before or after).

    Since all trades are generated at once, we check if any sell-at-loss in a
    taxable account has a corresponding buy of the same or similar ticker
    in ANY account.

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

    if not taxable_sells or not all_buys:
        return warnings

    # Build set of tickers being bought (including similar tickers)
    buy_tickers: set[str] = set()
    for t in all_buys:
        buy_tickers.add(t.ticker)

    for sell in taxable_sells:
        # Check if this is a loss sale (indicated by TLH in reasoning)
        is_loss_sale = "TLH" in sell.reasoning or any(
            "loss" in w.lower() for w in sell.warnings
        )
        if not is_loss_sale:
            continue

        sell_ticker = sell.ticker
        sell_info = mapping.get(sell_ticker)

        # Check if same ticker is being bought
        if sell_ticker in buy_tickers:
            buy_accounts = [
                t.account_name for t in all_buys if t.ticker == sell_ticker
            ]
            warnings.append(
                f"WASH SALE RISK: Selling {sell_ticker} at a loss in "
                f"{sell.account_name} while buying {sell_ticker} in "
                f"{', '.join(buy_accounts)}. The loss may be disallowed."
            )

        # Check similar tickers
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
