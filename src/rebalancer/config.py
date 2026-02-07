from decimal import Decimal
from pathlib import Path

import yaml

from .models import AccountType, AllocationTarget, RebalanceConfig, TickerMapping


def load_targets(path: Path) -> list[AllocationTarget]:
    """Load target allocation percentages from YAML."""
    with open(path) as f:
        data = yaml.safe_load(f)

    targets = []
    for asset_class, pct in data.items():
        targets.append(
            AllocationTarget(asset_class=asset_class, target_pct=Decimal(str(pct)))
        )

    total = sum(t.target_pct for t in targets)
    if total != Decimal("100"):
        raise ValueError(f"Target allocations must sum to 100, got {total}")

    return targets


def load_mapping(path: Path) -> dict[str, TickerMapping]:
    """Load ticker-to-asset-class mapping from YAML."""
    with open(path) as f:
        data = yaml.safe_load(f)

    mappings = {}
    for ticker, info in data.items():
        if isinstance(info, str):
            mappings[ticker] = TickerMapping(asset_class=info)
        else:
            mappings[ticker] = TickerMapping(
                asset_class=info["asset_class"],
                similar_tickers=info.get("similar", []),
            )

    return mappings


def load_config(path: Path) -> RebalanceConfig:
    """Load rebalancing configuration from YAML."""
    with open(path) as f:
        data = yaml.safe_load(f)

    rebalance = data.get("rebalance", {})
    tax = data.get("tax", {})
    accounts_raw = data.get("accounts", {})

    account_mappings = {}
    for substr, acct_type_str in accounts_raw.items():
        account_mappings[substr] = AccountType(acct_type_str)

    return RebalanceConfig(
        threshold_pct=Decimal(str(rebalance.get("threshold_pct", 3.0))),
        min_trade_value=Decimal(str(rebalance.get("min_trade_value", 50))),
        tlh_enabled=tax.get("tlh_enabled", True),
        avoid_gains_in_taxable=tax.get("avoid_gains_in_taxable", True),
        cash_to_invest=Decimal(str(data.get("cash_to_invest", 0))),
        account_mappings=account_mappings,
    )
