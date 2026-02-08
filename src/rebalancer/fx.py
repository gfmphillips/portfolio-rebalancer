"""Foreign exchange helpers and bank cash account support."""

import time
from decimal import Decimal
from typing import Literal

import requests
from pydantic import BaseModel

from rebalancer.models import AccountType, Position

# ---------------------------------------------------------------------------
# Model
# ---------------------------------------------------------------------------


class BankCashAccount(BaseModel):
    currency: Literal["USD", "EUR"]
    amount: Decimal
    account_name: str


# ---------------------------------------------------------------------------
# FX rate fetching (with 5-minute in-memory cache)
# ---------------------------------------------------------------------------

_fx_cache: dict[str, tuple[float, Decimal]] = {}
_FX_CACHE_TTL = 300  # seconds


def fetch_fx_rate(base: str = "EUR", target: str = "USD") -> Decimal | None:
    """Fetch a live FX rate from the Frankfurter API (free, no key required).

    Returns the rate as a Decimal, or None on failure.
    Results are cached in-memory for 5 minutes.
    """
    cache_key = f"{base}_{target}"
    now = time.monotonic()

    if cache_key in _fx_cache:
        cached_time, cached_rate = _fx_cache[cache_key]
        if now - cached_time < _FX_CACHE_TTL:
            return cached_rate

    try:
        resp = requests.get(
            "https://api.frankfurter.app/latest",
            params={"from": base, "to": target},
            timeout=5,
        )
        resp.raise_for_status()
        data = resp.json()
        rate = Decimal(str(data["rates"][target]))
        _fx_cache[cache_key] = (now, rate)
        return rate
    except Exception:
        return None


def _clear_fx_cache() -> None:
    """Clear the FX rate cache (for testing)."""
    _fx_cache.clear()


# ---------------------------------------------------------------------------
# Conversion
# ---------------------------------------------------------------------------


def convert_bank_cash_to_positions(
    accounts: list[BankCashAccount],
    eur_usd_rate: Decimal,
) -> list[Position]:
    """Convert bank cash accounts into synthetic Position objects.

    USD accounts get price=1; EUR accounts use the supplied EUR/USD rate.
    All positions use account_type=TAXABLE.
    """
    positions: list[Position] = []
    for acct in accounts:
        if acct.amount <= 0:
            continue
        if acct.currency == "USD":
            positions.append(
                Position(
                    account_name=acct.account_name,
                    account_type=AccountType.TAXABLE,
                    ticker="CASH-USD",
                    description="Bank Cash (USD)",
                    quantity=acct.amount,
                    price=Decimal("1"),
                    market_value=acct.amount,
                    cost_basis_total=None,
                )
            )
        else:  # EUR
            usd_value = (acct.amount * eur_usd_rate).quantize(Decimal("0.01"))
            positions.append(
                Position(
                    account_name=acct.account_name,
                    account_type=AccountType.TAXABLE,
                    ticker="CASH-EUR",
                    description="Bank Cash (EUR)",
                    quantity=acct.amount,
                    price=eur_usd_rate,
                    market_value=usd_value,
                    cost_basis_total=None,
                )
            )
    return positions
