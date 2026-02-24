"""Stock basket module — standalone, no Waypoint dependency.

Load a user-provided CSV, normalize weights, and compute top-up-underweight orders.

CSV format:
    ticker,target_weight[,name,sector,country,is_adr]

Weights may be 0–1 (fractional) or 0–100 (percentage); auto-detected.
All weights are normalized to sum=1 before use.
"""

import csv
import io
from decimal import ROUND_DOWN, ROUND_HALF_UP, Decimal
from pathlib import Path

from .models import (
    AccountType,
    BasketConstituent,
    InstrumentType,
    Position,
    Trade,
    ZERO,
)

TEMPLATE_HEADER = "ticker,target_weight,price,name,sector,country,is_adr"
REQUIRED_COLUMNS = {"ticker", "target_weight"}

_ONE = Decimal("1")
_HUNDRED = Decimal("100")


def basket_template_csv() -> str:
    """Return a starter CSV template the user can fill in."""
    lines = [
        TEMPLATE_HEADER,
        "# Weights: 0-100 (percentages) or 0-1 (fractions); auto-normalized.",
        "# price: optional — fill in to enable automatic share calculation.",
        "# Remove comment lines before uploading.",
        "AAPL,7.00,189.50,Apple Inc.,Technology,US,false",
        "MSFT,6.50,415.25,Microsoft Corp.,Technology,US,false",
        "AMZN,5.00,196.80,Amazon.com Inc.,Consumer Discretionary,US,false",
        "GOOGL,4.50,175.60,Alphabet Inc.,Communication Services,US,false",
        "BRK.B,3.00,449.00,Berkshire Hathaway B,Financials,US,false",
    ]
    return "\n".join(lines) + "\n"


def load_basket_csv(
    path: Path | None = None,
    csv_text: str | None = None,
) -> list[BasketConstituent]:
    """Load and validate a basket CSV.

    Args:
        path: Path to a CSV file on disk.
        csv_text: Raw CSV text (used when path is None, e.g. from Streamlit paste area).

    Returns:
        List of BasketConstituent with target_weight normalized to sum=1.

    Raises:
        ValueError: On missing columns, duplicate tickers, or invalid weights.
    """
    if path is not None:
        text = Path(path).read_text(encoding="utf-8")
    elif csv_text is not None:
        text = csv_text
    else:
        raise ValueError("Must supply either path or csv_text.")

    # Strip comment lines (start with #)
    lines = [ln for ln in text.splitlines() if not ln.strip().startswith("#")]
    text = "\n".join(lines)

    reader = csv.DictReader(io.StringIO(text))
    if reader.fieldnames is None:
        raise ValueError("CSV appears empty or has no header row.")

    header_lower = {f.strip().lower() for f in reader.fieldnames}
    missing = REQUIRED_COLUMNS - header_lower
    if missing:
        raise ValueError(f"CSV missing required column(s): {', '.join(sorted(missing))}")

    rows = list(reader)
    if not rows:
        raise ValueError("CSV has a header but no data rows.")

    raw: list[tuple[str, Decimal, dict]] = []
    seen_tickers: set[str] = set()

    for i, row in enumerate(rows, start=2):
        ticker = row.get("ticker", "").strip().upper()
        if not ticker:
            continue  # skip blank rows

        if ticker in seen_tickers:
            raise ValueError(f"Duplicate ticker '{ticker}' at row {i}.")
        seen_tickers.add(ticker)

        try:
            w = Decimal(str(row.get("target_weight", "").strip()))
        except Exception:
            raise ValueError(
                f"Invalid target_weight for ticker '{ticker}' at row {i}."
            )
        if w < ZERO:
            raise ValueError(
                f"Negative weight for ticker '{ticker}' at row {i}."
            )

        # Optional price column
        price_raw = row.get("price", "").strip()
        price_val: Decimal | None = None
        if price_raw:
            try:
                p = Decimal(price_raw.replace("$", "").replace(",", ""))
                if p > ZERO:
                    price_val = p
            except Exception:
                pass  # ignore unparseable price; leave as None

        extras = {
            "name":    row.get("name", "").strip(),
            "sector":  row.get("sector", "").strip(),
            "country": row.get("country", "").strip(),
            "is_adr":  row.get("is_adr", "false").strip().lower() in {"true", "1", "yes"},
            "price":   price_val,
        }
        raw.append((ticker, w, extras))

    if not raw:
        raise ValueError("No valid rows found in basket CSV.")

    # Auto-detect weight scale: if max weight > 1, treat as 0-100 percentages
    max_w = max(w for _, w, _ in raw)
    if max_w > _ONE:
        raw = [(t, w / _HUNDRED, e) for t, w, e in raw]

    total_w = sum(w for _, w, _ in raw)
    if total_w <= ZERO:
        raise ValueError("All weights are zero — cannot normalize.")

    constituents: list[BasketConstituent] = []
    for ticker, w, extras in raw:
        normalized = (w / total_w).quantize(Decimal("0.000001"), rounding=ROUND_HALF_UP)
        constituents.append(
            BasketConstituent(
                ticker=ticker,
                target_weight=normalized,
                **extras,
            )
        )

    return constituents


def compute_basket_orders(
    constituents: list[BasketConstituent],
    current_holdings: dict[str, Position],
    equity_cash: Decimal,
    n_stocks: int,
    min_trade_value: Decimal,
    prices: dict[str, Decimal],
    account_name: str,
    account_type: AccountType,
) -> list[Trade]:
    """Compute top-up-underweight buy orders for the basket.

    Price resolution (in order of priority):
        1. prices dict (live or user-supplied externally)
        2. constituent.price (from CSV ``price`` column)
        3. No price → output a dollar-allocation row (shares=0, estimated_value=dollar_target,
           reasoning="No price — allocate $X manually")

    Algorithm — top-up-underweights:
        1. Select top-N constituents by target_weight; renormalize.
        2. V = current market value of basket holdings (in current_holdings).
        3. target_value[i] = w_i * (V + equity_cash)
        4. current_value[i] = held_shares * price  (0 if not held)
        5. additional_needed[i] = max(0, target_value[i] - current_value[i])
        6a. If price known: shares[i] = floor(additional_needed / price); skip if cost < min_trade_value
        6b. If no price: emit a dollar-allocation placeholder row (shares=0)
    """
    if not constituents or equity_cash < ZERO:
        return []

    # Select top-N by target_weight
    sorted_by_weight = sorted(constituents, key=lambda c: c.target_weight, reverse=True)
    top_n = sorted_by_weight[:n_stocks]

    # Renormalize weights of selected subset
    total_w = sum(c.target_weight for c in top_n)
    if total_w <= ZERO:
        return []

    selected = [
        (c, (c.target_weight / total_w).quantize(Decimal("0.000001"), rounding=ROUND_HALF_UP))
        for c in top_n
    ]

    # V = current market value of basket positions in current_holdings
    basket_tickers = {c.ticker for c, _ in selected}
    v = sum(
        pos.market_value
        for ticker, pos in current_holdings.items()
        if ticker in basket_tickers
    )

    combined_target = v + equity_cash
    trades: list[Trade] = []

    for constituent, weight in selected:
        ticker = constituent.ticker

        # Resolve price: external dict → constituent CSV column → None
        price = prices.get(ticker)
        if price is None or price <= ZERO:
            price = constituent.price if constituent.price and constituent.price > ZERO else None

        target_value = (weight * combined_target).quantize(
            Decimal("0.01"), rounding=ROUND_HALF_UP
        )
        current_pos = current_holdings.get(ticker)
        current_value = current_pos.market_value if current_pos else ZERO

        additional_needed = max(ZERO, target_value - current_value)
        if additional_needed <= ZERO:
            continue

        if price is not None and price > ZERO:
            # Full share calculation
            shares = (additional_needed / price).to_integral_value(rounding=ROUND_DOWN)
            if shares <= ZERO:
                continue
            actual_cost = (shares * price).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)
            if actual_cost < min_trade_value:
                continue
            trades.append(Trade(
                account_name=account_name,
                account_type=account_type,
                ticker=ticker,
                action="BUY",
                shares=shares,
                estimated_value=actual_cost,
                reasoning=(
                    f"Basket top-up: target ${target_value}, "
                    f"current ${current_value}, buy {shares} shares @ ${price}"
                ),
            ))
        else:
            # No price available — emit dollar-allocation placeholder
            if additional_needed < min_trade_value:
                continue
            trades.append(Trade(
                account_name=account_name,
                account_type=account_type,
                ticker=ticker,
                action="BUY",
                shares=ZERO,
                estimated_value=additional_needed,
                reasoning=(
                    f"No price — allocate ${additional_needed} manually "
                    f"(target ${target_value}, current ${current_value})"
                ),
            ))

    return trades
