"""Live market price fetching via yfinance."""
from __future__ import annotations

import logging
from decimal import Decimal

logger = logging.getLogger(__name__)

# These tickers are always pegged to $1.00/share — no network call needed.
MONEY_MARKET_TICKERS: frozenset[str] = frozenset(
    {
        "SPAXX", "FDRXX", "FCASH", "SPRXX",
        "VMFXX", "VMMXX", "FDLXX", "FZDXX",
        "FNSXX", "SNSXX", "TFDXX",
    }
)


def fetch_prices(tickers: list[str]) -> dict[str, Decimal]:
    """Fetch current market prices for a list of tickers.

    Returns a dict mapping ticker -> Decimal price.  Tickers that cannot be
    priced are omitted from the result so callers can detect failures.
    Money-market tickers are returned as $1.0000 without a network call.
    Synthetic CASH-* tickers (internal bank-cash positions) are skipped.
    """
    try:
        import yfinance as yf
    except ImportError:
        logger.warning("yfinance is not installed; cannot fetch live prices.")
        return {}

    prices: dict[str, Decimal] = {}
    to_fetch: list[str] = []

    for ticker in tickers:
        if ticker.startswith("CASH-"):
            continue  # synthetic runtime tickers have no market price
        if ticker in MONEY_MARKET_TICKERS:
            prices[ticker] = Decimal("1.0000")
        else:
            to_fetch.append(ticker)

    if not to_fetch:
        return prices

    # ------------------------------------------------------------------ #
    # Attempt a single batch download (one HTTP round-trip).              #
    # yfinance returns a flat DataFrame for one ticker, MultiIndex for    #
    # multiple — we handle both.                                          #
    # ------------------------------------------------------------------ #
    still_needed: list[str] = list(to_fetch)
    try:
        raw = yf.download(
            to_fetch,
            period="1d",
            interval="2m",  # intraday bars: most recent price during the current session
            auto_adjust=True,
            progress=False,
        )
        if not raw.empty:
            # yfinance 1.x always returns a DataFrame for Close, with tickers
            # as columns regardless of whether one or many tickers were requested.
            close = raw["Close"]
            for ticker in list(still_needed):
                col = close.get(ticker)
                if col is not None:
                    last = col.dropna()
                    if not last.empty:
                        prices[ticker] = Decimal(
                            str(round(float(last.iloc[-1]), 4))
                        )
                        still_needed.remove(ticker)
    except Exception as exc:
        logger.warning(
            "Batch price fetch failed (%s); falling back to individual lookups.", exc
        )

    # ------------------------------------------------------------------ #
    # Individual fallback for anything the batch missed.                  #
    # ------------------------------------------------------------------ #
    for ticker in still_needed:
        try:
            t = yf.Ticker(ticker)
            raw_price = t.fast_info.last_price
            if raw_price and raw_price > 0:
                prices[ticker] = Decimal(str(round(float(raw_price), 4)))
            else:
                logger.debug("No price returned for %s", ticker)
        except Exception as exc:
            logger.debug("Individual price fetch failed for %s: %s", ticker, exc)

    return prices
