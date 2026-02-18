import warnings
from decimal import Decimal
from pathlib import Path

import yaml

from .models import (
    HUNDRED,
    ZERO,
    AccountType,
    AllocationTarget,
    CashCategory,
    CashConfig,
    ConstraintsConfig,
    GermanTaxConfig,
    OutputConfig,
    PrecisionConfig,
    RebalanceConfig,
    SortKey,
    TickerMapping,
    UnifiedConfig,
)


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
    if total != HUNDRED:
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
            raw_price = info.get("price", None)
            mappings[ticker] = TickerMapping(
                asset_class=info["asset_class"],
                similar_tickers=info.get("similar", []),
                domicile=info.get("domicile", "US"),
                preferred=info.get("preferred", False),
                consolidate_to=info.get("consolidate_to", None),
                price=Decimal(str(raw_price)) if raw_price is not None else None,
                german_fund_category=info.get("german_fund_category", None),
                is_accumulating=info.get("is_accumulating", None),
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
    valid_types = {e.value for e in AccountType}
    for substr, acct_type_str in accounts_raw.items():
        if acct_type_str not in valid_types:
            raise ValueError(
                f"Invalid account type '{acct_type_str}' for '{substr}'. "
                f"Valid types: {', '.join(sorted(valid_types))}"
            )
        account_mappings[substr] = AccountType(acct_type_str)

    return RebalanceConfig(
        threshold_pct=Decimal(str(rebalance.get("threshold_pct", 3.0))),
        min_trade_value=Decimal(str(rebalance.get("min_trade_value", 50))),
        tlh_enabled=tax.get("tlh_enabled", True),
        avoid_gains_in_taxable=tax.get("avoid_gains_in_taxable", True),
        cash_to_invest=Decimal(str(data.get("cash_to_invest", 0))),
        account_mappings=account_mappings,
    )


def is_unified_config(path: Path) -> bool:
    """Check whether a config file uses the unified format (has 'allocation' key)."""
    with open(path) as f:
        data = yaml.safe_load(f)
    return isinstance(data, dict) and "allocation" in data


def load_unified_config(
    path: Path,
) -> UnifiedConfig:
    """Parse a unified config YAML and return a UnifiedConfig."""
    with open(path) as f:
        data = yaml.safe_load(f)

    # --- allocation → list[AllocationTarget] ---
    allocation_raw = data.get("allocation", {})
    targets = []
    for asset_class, pct in allocation_raw.items():
        targets.append(
            AllocationTarget(asset_class=asset_class, target_pct=Decimal(str(pct)))
        )
    total = sum(t.target_pct for t in targets)
    if total != HUNDRED:
        raise ValueError(f"Target allocations must sum to 100, got {total}")

    # --- rebalance → threshold_pct, min_trade_value ---
    rebalance = data.get("rebalance", {})

    # --- tax → tlh_enabled + avoid_gains_in_taxable ---
    tax = data.get("tax", {})
    tax_enabled = tax.get("enabled", False)
    tlh_enabled = tax.get("tlh_enabled", tax_enabled)
    avoid_gains = tax.get("avoid_gains_in_taxable", tax_enabled)

    # --- accounts → account_mappings ---
    accounts_raw = data.get("accounts", {})
    account_mappings: dict[str, AccountType] = {}
    valid_types = {e.value for e in AccountType}
    for substr, acct_type_str in accounts_raw.items():
        if acct_type_str not in valid_types:
            raise ValueError(
                f"Invalid account type '{acct_type_str}' for '{substr}'. "
                f"Valid types: {', '.join(sorted(valid_types))}"
            )
        account_mappings[substr] = AccountType(acct_type_str)

    rebalance_config = RebalanceConfig(
        threshold_pct=Decimal(str(rebalance.get("threshold_pct", 5.0))),
        threshold_relative_pct=Decimal(str(rebalance.get("threshold_relative_pct", 20))),
        min_trade_value=Decimal(str(rebalance.get("min_trade_value", 500))),
        tlh_enabled=tlh_enabled,
        avoid_gains_in_taxable=avoid_gains,
        cash_to_invest=ZERO,
        account_mappings=account_mappings,
    )

    # --- cash → CashConfig ---
    cash_raw = data.get("cash", {})
    eurusd_fx = Decimal(str(cash_raw.get("eurusd_fx", "1.10")))

    if "investable" in cash_raw or "emergency" in cash_raw:
        # New format
        inv_raw = cash_raw.get("investable", {})
        emg_raw = cash_raw.get("emergency", {})
        cash_config = CashConfig(
            eurusd_fx=eurusd_fx,
            investable=CashCategory(
                eur=Decimal(str(inv_raw.get("eur", 0))),
                usd=Decimal(str(inv_raw.get("usd", 0))),
            ),
            emergency=CashCategory(
                eur=Decimal(str(emg_raw.get("eur", 0))),
                usd=Decimal(str(emg_raw.get("usd", 0))),
            ),
        )
    elif "external_cash_eur" in cash_raw or "external_cash_usd" in cash_raw or "include_in_portfolio" in cash_raw:
        # Legacy format — migrate
        warnings.warn(
            "cash.external_cash_eur/usd and include_in_portfolio are deprecated. "
            "Use cash.investable and cash.emergency instead.",
            DeprecationWarning,
            stacklevel=2,
        )
        eur = Decimal(str(cash_raw.get("external_cash_eur", 0)))
        usd = Decimal(str(cash_raw.get("external_cash_usd", 0)))
        include = cash_raw.get("include_in_portfolio", True)
        if include:
            cash_config = CashConfig(
                eurusd_fx=eurusd_fx,
                investable=CashCategory(eur=eur, usd=usd),
                include_in_portfolio=True,
                external_cash_eur=eur,
                external_cash_usd=usd,
            )
        else:
            cash_config = CashConfig(
                eurusd_fx=eurusd_fx,
                emergency=CashCategory(eur=eur, usd=usd),
                include_in_portfolio=False,
                external_cash_eur=eur,
                external_cash_usd=usd,
            )
    else:
        cash_config = CashConfig(eurusd_fx=eurusd_fx)

    # --- output → OutputConfig ---
    output_raw = data.get("output", {})
    precision_raw = output_raw.get("precision", {})
    precision = PrecisionConfig(
        currency=precision_raw.get("currency", 0),
        pct=precision_raw.get("pct", 2),
    )
    sort_order_raw = output_raw.get("sort_order", ["sells_first", "largest_trade_first"])
    sort_order = [SortKey(s) for s in sort_order_raw]
    output_config = OutputConfig(
        show_only_actionable_trades=output_raw.get("show_only_actionable_trades", True),
        sort_order=sort_order,
        precision=precision,
    )

    # --- german_tax → GermanTaxConfig ---
    gt_raw = data.get("german_tax", {})
    german_tax_config = GermanTaxConfig(
        enabled=gt_raw.get("enabled", False),
        filing_status=gt_raw.get("filing_status", "single"),
        kirchensteuer=gt_raw.get("kirchensteuer", False),
    )

    # --- constraints → ConstraintsConfig ---
    constraints_raw = data.get("constraints", {})
    min_bonds = constraints_raw.get("min_taxable_bonds_usd", None)
    constraints_config = ConstraintsConfig(
        min_taxable_bonds_usd=Decimal(str(min_bonds)) if min_bonds is not None else None,
    )

    return UnifiedConfig(
        targets=targets,
        rebalance_config=rebalance_config,
        output_config=output_config,
        cash_config=cash_config,
        german_tax_config=german_tax_config,
        constraints_config=constraints_config,
    )
