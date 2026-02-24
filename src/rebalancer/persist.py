"""Settings persistence for the portfolio rebalancer.

Saves/loads PolicyConfig and per-ticker instrument_type/never_want overrides
to/from ~/.portfolio-rebalancer/settings.json.

Design mirrors ~/waypoint/src/waypoint/persist.py.
"""

from __future__ import annotations

import json
from decimal import Decimal
from pathlib import Path

from .models import (
    AccountType,
    DefensiveMode,
    InstrumentType,
    PolicyConfig,
    TickerMapping,
)

_SETTINGS_DIR = Path.home() / ".portfolio-rebalancer"
_SETTINGS_FILE = _SETTINGS_DIR / "settings.json"


def _decimal(v: object, default: Decimal) -> Decimal:
    try:
        return Decimal(str(v))
    except Exception:
        return default


def load_settings() -> dict:
    """Load raw settings dict from disk.  Returns {} if file missing or corrupt."""
    if not _SETTINGS_FILE.exists():
        return {}
    try:
        return json.loads(_SETTINGS_FILE.read_text(encoding="utf-8"))
    except Exception:
        return {}


def save_settings(data: dict) -> None:
    """Write settings dict to disk atomically (temp file + rename)."""
    _SETTINGS_DIR.mkdir(parents=True, exist_ok=True)
    tmp = _SETTINGS_FILE.with_suffix(".tmp")
    tmp.write_text(json.dumps(data, indent=2, default=str), encoding="utf-8")
    tmp.replace(_SETTINGS_FILE)


def policy_from_settings(raw: dict) -> PolicyConfig:
    """Deserialize a PolicyConfig from the settings dict."""
    pc = raw.get("policy", {})
    return PolicyConfig(
        target_stock_pct=_decimal(pc.get("target_stock_pct"), Decimal("0.80")),
        target_bond_pct=_decimal(pc.get("target_bond_pct"), Decimal("0.20")),
        target_us_equity_pct_of_equity=_decimal(
            pc.get("target_us_equity_pct_of_equity"), Decimal("0.60")
        ),
        bank_cash_target_eur=_decimal(pc.get("bank_cash_target_eur"), Decimal("100000")),
        investable_cash_eur=_decimal(pc.get("investable_cash_eur"), Decimal("0")),
        monthly_investable_cash_eur=_decimal(
            pc.get("monthly_investable_cash_eur"), Decimal("0")
        ),
        eurusd_fx=_decimal(pc.get("eurusd_fx"), Decimal("1.10")),
        rebalance_band_abs=_decimal(pc.get("rebalance_band_abs"), Decimal("0.05")),
        horizon_months=int(pc.get("horizon_months", 18)),
        basket_size=int(pc.get("basket_size", 75)),
        min_trade_value=_decimal(pc.get("min_trade_value"), Decimal("50")),
        allow_international_basket=bool(pc.get("allow_international_basket", False)),
        allow_legacy_etf_sales=bool(pc.get("allow_legacy_etf_sales", False)),
        buy_enabled_account_types=frozenset(
            pc.get("buy_enabled_account_types", ["taxable"])
        ),
        defensive_mode=DefensiveMode(
            pc.get("defensive_mode", DefensiveMode.treasury_only.value)
        ),
        treasury_pct=_decimal(pc.get("treasury_pct"), Decimal("1.00")),
        cd_pct=_decimal(pc.get("cd_pct"), Decimal("0.00")),
        ladder_rungs_months=list(pc.get("ladder_rungs_months", [6, 12, 24, 36])),
        ladder_currency=str(pc.get("ladder_currency", "EUR")),
        basket_csv_path=pc.get("basket_csv_path"),
        basket_version=pc.get("basket_version"),
    )


def policy_to_settings(policy: PolicyConfig) -> dict:
    """Serialize a PolicyConfig to a JSON-safe dict."""
    return {
        "target_stock_pct":              str(policy.target_stock_pct),
        "target_bond_pct":               str(policy.target_bond_pct),
        "target_us_equity_pct_of_equity": str(policy.target_us_equity_pct_of_equity),
        "bank_cash_target_eur":          str(policy.bank_cash_target_eur),
        "investable_cash_eur":           str(policy.investable_cash_eur),
        "monthly_investable_cash_eur":   str(policy.monthly_investable_cash_eur),
        "eurusd_fx":                     str(policy.eurusd_fx),
        "rebalance_band_abs":            str(policy.rebalance_band_abs),
        "horizon_months":                policy.horizon_months,
        "basket_size":                   policy.basket_size,
        "min_trade_value":               str(policy.min_trade_value),
        "allow_international_basket":    policy.allow_international_basket,
        "allow_legacy_etf_sales":        policy.allow_legacy_etf_sales,
        "buy_enabled_account_types":     sorted(policy.buy_enabled_account_types),
        "defensive_mode":                policy.defensive_mode.value,
        "treasury_pct":                  str(policy.treasury_pct),
        "cd_pct":                        str(policy.cd_pct),
        "ladder_rungs_months":           policy.ladder_rungs_months,
        "ladder_currency":               policy.ladder_currency,
        "basket_csv_path":               policy.basket_csv_path,
        "basket_version":                policy.basket_version,
    }


def ticker_overrides_from_settings(raw: dict) -> dict[str, dict]:
    """Return the per-ticker override dict: {ticker: {instrument_type, never_want}}."""
    return raw.get("ticker_overrides", {})


def apply_ticker_overrides(
    mapping: dict[str, TickerMapping],
    overrides: dict[str, dict],
) -> dict[str, TickerMapping]:
    """Return a new mapping dict with instrument_type/never_want overrides applied."""
    result = {}
    for ticker, tm in mapping.items():
        ov = overrides.get(ticker, {})
        if not ov:
            result[ticker] = tm
            continue
        updates: dict = {}
        if "instrument_type" in ov:
            try:
                updates["instrument_type"] = InstrumentType(ov["instrument_type"])
            except ValueError:
                pass
        if "never_want" in ov:
            updates["never_want"] = bool(ov["never_want"])
        result[ticker] = tm.model_copy(update=updates) if updates else tm
    return result


def save_ticker_overrides(
    overrides: dict[str, dict],
    raw: dict | None = None,
) -> None:
    """Persist per-ticker overrides, merging with existing settings."""
    data = raw if raw is not None else load_settings()
    data["ticker_overrides"] = overrides
    save_settings(data)


def save_policy(policy: PolicyConfig, raw: dict | None = None) -> None:
    """Persist a PolicyConfig, merging with existing settings."""
    data = raw if raw is not None else load_settings()
    data["policy"] = policy_to_settings(policy)
    save_settings(data)
