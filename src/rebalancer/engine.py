from collections import defaultdict
from dataclasses import dataclass, field
from datetime import datetime, timezone
from decimal import ROUND_DOWN, ROUND_HALF_UP, Decimal
from importlib.metadata import version as pkg_version

from .models import (
    DEFENSIVE_ASSET_CLASSES,
    HUNDRED,
    STOCK_ASSET_CLASSES,
    ZERO,
    AccountType,
    AllocationTarget,
    AllocationView,
    BasketConstituent,
    BuyPlan,
    ConsolidationAnalysis,
    ConsolidationOpportunity,
    ConstraintCheck,
    ConstraintsConfig,
    DefensiveMode,
    PolicyConfig,
    Position,
    RebalanceConfig,
    RebalanceResult,
    RunMetadata,
    TAX_ADVANTAGED,
    TaxImpact,
    TaxLot,
    TickerMapping,
    Trade,
    Transaction,
)
from .tlh import check_wash_sales, find_tlh_opportunities

def build_run_metadata(eurusd_fx: Decimal) -> RunMetadata:
    """Create a RunMetadata snapshot for the current run."""
    try:
        ver = pkg_version("portfolio-rebalancer")
    except Exception:
        ver = "unknown"
    return RunMetadata(
        timestamp=datetime.now(timezone.utc).isoformat(),
        eurusd_fx_used=eurusd_fx,
        tool_version=ver,
    )


@dataclass
class CashPools:
    """Tracks available cash per funding boundary.

    Taxable accounts share one pool (money can move between them).
    Each tax-advantaged account is isolated.
    """

    taxable_pool: Decimal = ZERO
    tax_adv_pools: dict[str, Decimal] = field(default_factory=dict)

    def available(self, account_name: str, account_type: AccountType) -> Decimal:
        if account_type in TAX_ADVANTAGED:
            return self.tax_adv_pools.get(account_name, ZERO)
        return self.taxable_pool

    def add(self, account_name: str, account_type: AccountType, amount: Decimal) -> None:
        if account_type in TAX_ADVANTAGED:
            self.tax_adv_pools[account_name] = (
                self.tax_adv_pools.get(account_name, ZERO) + amount
            )
        else:
            self.taxable_pool += amount

    def spend(self, account_name: str, account_type: AccountType, amount: Decimal) -> None:
        if account_type in TAX_ADVANTAGED:
            self.tax_adv_pools[account_name] = (
                self.tax_adv_pools.get(account_name, ZERO) - amount
            )
        else:
            self.taxable_pool -= amount


def _quantize_pct(value: Decimal) -> Decimal:
    return value.quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)


def _quantize_shares(value: Decimal, whole_shares: bool = False) -> Decimal:
    if whole_shares:
        return value.quantize(Decimal("1"), rounding=ROUND_DOWN)
    return value.quantize(Decimal("0.001"), rounding=ROUND_HALF_UP)


def check_constraints(
    positions: list[Position],
    trades: list[Trade],
    mapping: dict[str, TickerMapping],
    constraints: ConstraintsConfig,
) -> list[ConstraintCheck]:
    """Check post-trade constraints and return a list of ConstraintCheck results.

    Constraints are checked but NOT enforced — the engine generates trades first,
    then we report whether the result violates any constraints.
    """
    checks: list[ConstraintCheck] = []

    if constraints.min_taxable_bonds_usd is not None:
        # Current taxable bond value
        taxable_bond_value = ZERO
        for p in positions:
            tm = mapping.get(p.ticker)
            if tm and tm.asset_class == "bonds" and p.account_type == AccountType.TAXABLE:
                taxable_bond_value += p.market_value

        # Subtract any bonds sold in taxable, add any bonds bought in taxable
        for t in trades:
            tm = mapping.get(t.ticker)
            if tm and tm.asset_class == "bonds" and t.account_type == AccountType.TAXABLE:
                if t.action == "SELL":
                    taxable_bond_value -= t.estimated_value
                elif t.action == "BUY":
                    taxable_bond_value += t.estimated_value

        post_trade_value = taxable_bond_value.quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)
        required = constraints.min_taxable_bonds_usd
        met = post_trade_value >= required
        message = (
            f"Post-trade taxable bonds: ${post_trade_value:,} "
            f"(minimum: ${required:,})"
        )
        if not met:
            message = f"CONSTRAINT VIOLATED: {message}"

        checks.append(ConstraintCheck(
            name="min_taxable_bonds_usd",
            required=required,
            actual=post_trade_value,
            met=met,
            message=message,
        ))

    return checks


def rebalance(
    positions: list[Position],
    targets: list[AllocationTarget],
    mapping: dict[str, TickerMapping],
    config: RebalanceConfig,
    metadata: RunMetadata | None = None,
    constraints: ConstraintsConfig | None = None,
    recent_transactions: list[Transaction] | None = None,
) -> RebalanceResult:
    """Run the full rebalancing algorithm."""
    warnings: list[str] = []

    # Total portfolio value
    total_value = sum(p.market_value for p in positions)
    if total_value == 0:
        return RebalanceResult(
            total_portfolio_value=ZERO,
            current_allocation={},
            target_allocation={t.asset_class: t.target_pct for t in targets},
            drift={},
            trades=[],
            warnings=["Portfolio has no value."],
            metadata=metadata,
        )

    effective_total = total_value + config.cash_to_invest

    # Current allocation by asset class
    value_by_class: dict[str, Decimal] = {}
    for p in positions:
        ticker_info = mapping.get(p.ticker)
        asset_class = ticker_info.asset_class if ticker_info else "unmapped"
        value_by_class[asset_class] = (
            value_by_class.get(asset_class, ZERO) + p.market_value
        )

    if "unmapped" in value_by_class:
        unmapped_tickers = {
            p.ticker for p in positions if p.ticker not in mapping
        }
        warnings.append(
            f"Unmapped tickers (excluded from rebalancing): {', '.join(sorted(unmapped_tickers))}"
        )

    # Warn about preferred tickers that aren't held and have no price in the mapping.
    # Without a price, buys cannot be routed to them.
    held_tickers = {p.ticker for p in positions}
    for ticker, info in mapping.items():
        if info.preferred and ticker not in held_tickers and info.price is None:
            warnings.append(
                f"Preferred ticker {ticker} ({info.asset_class}) is not in your portfolio "
                f"and has no 'price' set in the mapping. Buys cannot be routed to it. "
                f"Add 'price: <current_price>' to {ticker} in mapping.yaml."
            )

    # Warn about held positions with price=0 that will be silently skipped in buy routing.
    zero_price_mapped = sorted({
        p.ticker for p in positions
        if p.price == 0 and p.ticker in mapping and mapping[p.ticker].asset_class != "cash"
    })
    if zero_price_mapped:
        warnings.append(
            f"Positions with no price data (will be excluded from buy routing): "
            f"{', '.join(zero_price_mapped)}. Check your CSV or refresh live prices."
        )

    # Warn when a non-zero target allocation has no mapped tickers to buy into.
    for t in targets:
        if t.target_pct > 0:
            has_ticker = any(info.asset_class == t.asset_class for info in mapping.values())
            if not has_ticker:
                warnings.append(
                    f"No tickers are mapped to '{t.asset_class}' (target: {t.target_pct}%). "
                    "Add a fund in Fund Classification — the tool cannot buy into this class."
                )

    current_allocation: dict[str, Decimal] = {}
    for cls, val in value_by_class.items():
        current_allocation[cls] = _quantize_pct(val / total_value * HUNDRED)

    target_allocation: dict[str, Decimal] = {
        t.asset_class: t.target_pct for t in targets
    }

    # Drift: positive = overweight, negative = underweight
    all_classes = sorted(set(current_allocation.keys()) | set(target_allocation.keys()))
    drift: dict[str, Decimal] = {}
    for cls in all_classes:
        current = current_allocation.get(cls, ZERO)
        target = target_allocation.get(cls, ZERO)
        drift[cls] = _quantize_pct(current - target)

    # Calculate target dollar amounts based on effective total
    target_amounts: dict[str, Decimal] = {}
    for cls in all_classes:
        target_pct = target_allocation.get(cls, ZERO)
        target_amounts[cls] = effective_total * target_pct / HUNDRED

    # Dollar adjustment needed per class (negative = need to sell, positive = need to buy)
    adjustment_by_class: dict[str, Decimal] = {}
    for cls in all_classes:
        if cls == "unmapped":
            continue
        current_val = value_by_class.get(cls, ZERO)
        target_val = target_amounts.get(cls, ZERO)
        adj = target_val - current_val
        # Skip if drift below both thresholds (OR logic: breach either → rebalance)
        abs_drift = abs(drift.get(cls, ZERO))
        target_pct = target_allocation.get(cls, ZERO)
        rel_drift = (abs_drift / target_pct * HUNDRED) if target_pct > 0 else ZERO
        abs_ok = abs_drift < config.threshold_pct
        rel_ok = rel_drift < config.threshold_relative_pct
        if abs_ok and rel_ok and config.cash_to_invest == 0:
            continue
        if abs(adj) < config.min_trade_value:
            continue
        adjustment_by_class[cls] = adj

    # If we have cash to invest, we only buy (add to underweight classes proportionally)
    trades: list[Trade] = []
    if config.cash_to_invest > 0:
        trades.extend(
            _allocate_new_cash(
                config.cash_to_invest, positions, adjustment_by_class, mapping, config
            )
        )
    else:
        # Full rebalance: sell overweight, buy underweight
        # Tax-advantaged accounts first, then taxable
        trades.extend(
            _generate_rebalance_trades(
                positions, adjustment_by_class, mapping, config, warnings
            )
        )

    # Check for wash sales across all trades
    if config.tlh_enabled:
        wash_warnings = check_wash_sales(trades, mapping, recent_transactions)
        for tw in wash_warnings:
            warnings.append(tw)

    # Compute tax impact summary (only taxable sell trades matter)
    total_gains = ZERO
    total_losses = ZERO
    taxable_count = 0
    for t in trades:
        if (
            t.action == "SELL"
            and t.account_type == AccountType.TAXABLE
            and t.estimated_gain_loss is not None
        ):
            taxable_count += 1
            if t.estimated_gain_loss > 0:
                total_gains += t.estimated_gain_loss
            else:
                total_losses += t.estimated_gain_loss

    tax_impact = TaxImpact(
        estimated_total_gains=total_gains,
        estimated_total_losses=total_losses,
        estimated_net=(total_gains + total_losses).quantize(
            Decimal("0.01"), rounding=ROUND_HALF_UP
        ),
        taxable_trades_count=taxable_count,
    )

    # Check constraints
    constraint_checks: list[ConstraintCheck] = []
    if constraints is not None:
        constraint_checks = check_constraints(positions, trades, mapping, constraints)
        for cc in constraint_checks:
            if not cc.met:
                warnings.append(cc.message)

    return RebalanceResult(
        total_portfolio_value=total_value,
        current_allocation=current_allocation,
        target_allocation=target_allocation,
        drift=drift,
        trades=trades,
        warnings=warnings,
        tax_impact=tax_impact,
        constraints=constraint_checks,
        metadata=metadata,
    )


def _allocate_new_cash(
    cash: Decimal,
    positions: list[Position],
    adjustment_by_class: dict[str, Decimal],
    mapping: dict[str, TickerMapping],
    config: RebalanceConfig,
) -> list[Trade]:
    """Allocate new cash to underweight asset classes (buys only)."""
    trades: list[Trade] = []

    # Only buy into underweight classes
    underweight = {
        cls: amt for cls, amt in adjustment_by_class.items() if amt > 0
    }
    if not underweight:
        return trades

    # Proportional allocation of cash among underweight classes
    total_underweight = sum(underweight.values())
    cash_allocation: dict[str, Decimal] = {}
    for cls, amt in underweight.items():
        share = amt / total_underweight
        cash_allocation[cls] = (cash * share).quantize(
            Decimal("0.01"), rounding=ROUND_HALF_UP
        )

    # Find best ticker to buy per asset class
    for cls, buy_amount in cash_allocation.items():
        if buy_amount < config.min_trade_value:
            continue
        ticker, price, account = _pick_buy_ticker(cls, positions, mapping)
        if ticker and price > 0 and account:
            shares = _quantize_shares(buy_amount / price, whole_shares=config.whole_shares_only)
            if shares > 0:
                trades.append(
                    Trade(
                        account_name=account.account_name,
                        account_type=account.account_type,
                        ticker=ticker,
                        action="BUY",
                        shares=shares,
                        estimated_value=(shares * price).quantize(
                            Decimal("0.01"), rounding=ROUND_HALF_UP
                        ),
                        reasoning=f"Invest new cash into underweight {cls}",
                    )
                )

    return trades


def _pick_buy_ticker(
    asset_class: str,
    positions: list[Position],
    mapping: dict[str, TickerMapping],
) -> tuple[str | None, Decimal, Position | None]:
    """Pick the best ticker and account to buy for an asset class.

    Prefers buying in existing positions, preferring tax-advantaged accounts.
    Returns (ticker, price, representative_position).
    """
    # Find existing positions in this asset class
    candidates: list[Position] = []
    for p in positions:
        ticker_info = mapping.get(p.ticker)
        if ticker_info and ticker_info.asset_class == asset_class and p.price > 0:
            candidates.append(p)

    if not candidates:
        # Find any ticker mapped to this class
        for ticker, info in mapping.items():
            if info.asset_class == asset_class:
                return ticker, ZERO, None
        return None, ZERO, None

    # Prefer tax-advantaged accounts, then preferred (end-state) ticker
    tax_adv = [c for c in candidates if c.account_type in TAX_ADVANTAGED]
    pool = tax_adv if tax_adv else candidates
    preferred = [c for c in pool if mapping.get(c.ticker) and mapping[c.ticker].preferred]
    if preferred:
        best = preferred[0]
        return best.ticker, best.price, best

    # No preferred ticker held — check mapping for one with a price set
    for ticker, info in mapping.items():
        if info.asset_class == asset_class and info.preferred and info.price is not None:
            return ticker, info.price, pool[0]

    return pool[0].ticker, pool[0].price, pool[0]


EMERGENCY_TICKERS = {"CASH-USD-EMERGENCY", "CASH-EUR-EMERGENCY"}


def _build_initial_cash_pools(
    positions: list[Position],
    mapping: dict[str, TickerMapping],
) -> CashPools:
    """Build initial cash pools from cash positions.

    Emergency cash positions are excluded — they are visible in the portfolio
    total but not available for funding buys.
    """
    pools = CashPools()
    for p in positions:
        if p.ticker in EMERGENCY_TICKERS:
            continue
        ticker_info = mapping.get(p.ticker)
        if ticker_info and ticker_info.asset_class == "cash":
            pools.add(p.account_name, p.account_type, p.market_value)
    return pools


def _allocate_buys(
    asset_class: str,
    buy_amount: Decimal,
    positions: list[Position],
    mapping: dict[str, TickerMapping],
    config: RebalanceConfig,
    pools: CashPools,
) -> list[Trade]:
    """Allocate buys for an underweight asset class across accounts, respecting cash pools.

    Builds a unified candidate list of ALL accounts with available cash:
    - Accounts already holding this class buy their existing ticker.
    - Accounts without a position buy a reference ticker (largest position in the class).

    All candidates compete in one sorted list (tax-advantaged first, then by available
    cash descending), so sell proceeds in tax-advantaged accounts get deployed before
    taxable cash.

    Returns a list of Trade objects (may be multiple accounts for one asset class).
    """
    trades: list[Trade] = []

    # Find positions in this class with a tradeable price
    class_positions: list[Position] = []
    for p in positions:
        ticker_info = mapping.get(p.ticker)
        if ticker_info and ticker_info.asset_class == asset_class and p.price > 0:
            class_positions.append(p)

    if not class_positions:
        return trades

    # Reference ticker: prefer a preferred (end-state) ticker if one is held,
    # otherwise fall back to the largest existing position in this class.
    ref_ticker: str = class_positions[0].ticker
    ref_price: Decimal = class_positions[0].price
    best_ref_value = class_positions[0].market_value
    for p in class_positions[1:]:
        if p.market_value > best_ref_value:
            ref_ticker = p.ticker
            ref_price = p.price
            best_ref_value = p.market_value
    preferred_found = False
    for p in class_positions:
        ticker_info = mapping.get(p.ticker)
        if ticker_info and ticker_info.preferred:
            ref_ticker = p.ticker
            ref_price = p.price
            preferred_found = True
            break
    if not preferred_found:
        # Preferred ticker not held — check mapping for one with a usable price set
        for ticker, info in mapping.items():
            if info.asset_class == asset_class and info.preferred and info.price is not None and info.price > 0:
                ref_ticker = ticker
                ref_price = info.price
                break

    # Deduplicate by account: pick largest position per account
    best_by_account: dict[str, Position] = {}
    for p in class_positions:
        existing = best_by_account.get(p.account_name)
        if existing is None or p.market_value > existing.market_value:
            best_by_account[p.account_name] = p
    covered_accounts = set(best_by_account.keys())

    # Build unified candidate list: (account_name, account_type, ticker, price, is_new)
    all_candidates: list[tuple[str, AccountType, str, Decimal, bool]] = []

    for name, p in best_by_account.items():
        ticker_info = mapping.get(p.ticker)
        is_preferred = ticker_info.preferred if ticker_info else False
        # Route legacy-fund accounts to buy the preferred end-state ticker instead
        buy_ticker = p.ticker if is_preferred else ref_ticker
        buy_price = p.price if is_preferred else ref_price
        all_candidates.append((name, p.account_type, buy_ticker, buy_price, False))

    # Add accounts that have available cash but no position in this class
    seen_accounts: dict[str, Position] = {}
    for p in positions:
        if p.account_name not in seen_accounts:
            seen_accounts[p.account_name] = p
    for name, p in seen_accounts.items():
        if name not in covered_accounts:
            all_candidates.append((name, p.account_type, ref_ticker, ref_price, True))

    # Sort: tax-advantaged first, then by available cash descending
    def _sort_key(c: tuple[str, AccountType, str, Decimal, bool]) -> tuple[int, Decimal]:
        name, acct_type, _ticker, _price, _is_new = c
        is_tax_adv = 0 if acct_type in TAX_ADVANTAGED else 1
        return (is_tax_adv, -pools.available(name, acct_type))

    all_candidates.sort(key=_sort_key)

    remaining = buy_amount
    for name, acct_type, ticker, price, is_new in all_candidates:
        if remaining <= 0:
            break
        avail = pools.available(name, acct_type)
        if avail <= 0:
            continue
        buy_here = min(remaining, avail)
        if buy_here < config.min_trade_value:
            continue
        if price <= 0:
            continue  # no price available for this ticker; skip silently (warned at rebalance() level)
        shares = _quantize_shares(buy_here / price, whole_shares=config.whole_shares_only)
        if shares <= 0:
            continue
        actual_value = (shares * price).quantize(
            Decimal("0.01"), rounding=ROUND_HALF_UP
        )
        reasoning = f"Increase underweight {asset_class}"
        if is_new:
            reasoning += " (new position)"
        trades.append(
            Trade(
                account_name=name,
                account_type=acct_type,
                ticker=ticker,
                action="BUY",
                shares=shares,
                estimated_value=actual_value,
                reasoning=reasoning,
            )
        )
        pools.spend(name, acct_type, actual_value)
        remaining -= actual_value

    return trades


def _sort_lots(
    position: Position, config: RebalanceConfig
) -> list[TaxLot]:
    """Sort tax lots by the appropriate strategy for selling.

    - Taxable (HIFO / TLH): highest cost first — minimizes gain (HIFO) and
      maximizes harvested loss (TLH).  Both goals are served by selling the
      most-expensive lots first.
    - Tax-advantaged: FIFO (by acquisition date ascending)
    """
    lots = list(position.tax_lots)
    if position.account_type in TAX_ADVANTAGED:
        # FIFO: oldest first
        lots.sort(key=lambda lot: lot.acquisition_date)
    else:
        # Taxable (both HIFO and TLH): highest cost first
        lots.sort(key=lambda lot: lot.cost_basis_per_share, reverse=True)
    return lots


def _estimate_gain_loss(
    price: Decimal,
    shares: Decimal,
    cost_basis_per_share: Decimal | None,
    market_value: Decimal | None,
    cost_basis_total: Decimal | None,
    quantity: Decimal,
    account_type: AccountType,
    avoid_gains: bool,
) -> tuple[Decimal | None, list[str]]:
    """Estimate gain/loss and generate warnings for a sell trade.

    Uses per-share cost basis (lot-aware) when available, otherwise
    falls back to blended basis from position-level totals.

    Returns (estimated_gain_loss, warnings).
    """
    warnings: list[str] = []
    est: Decimal | None = None

    if cost_basis_per_share is not None:
        est = ((price - cost_basis_per_share) * shares).quantize(
            Decimal("0.01"), rounding=ROUND_HALF_UP
        )
    elif cost_basis_total is not None and market_value is not None and quantity > 0:
        gain_per_share = (market_value - cost_basis_total) / quantity
        est = (gain_per_share * shares).quantize(
            Decimal("0.01"), rounding=ROUND_HALF_UP
        )

    if (
        account_type == AccountType.TAXABLE
        and avoid_gains
        and est is not None
        and est > 0
    ):
        warnings.append(
            f"Selling at estimated gain of ${est} in taxable account"
        )

    return est, warnings


def _generate_rebalance_trades(
    positions: list[Position],
    adjustment_by_class: dict[str, Decimal],
    mapping: dict[str, TickerMapping],
    config: RebalanceConfig,
    warnings: list[str],
) -> list[Trade]:
    """Generate rebalance trades: sell overweight, buy underweight."""
    trades: list[Trade] = []

    # Build cash pools from cash positions
    pools = _build_initial_cash_pools(positions, mapping)

    # Group positions by asset class and account type
    positions_by_class: defaultdict[str, list[Position]] = defaultdict(list)
    for p in positions:
        ticker_info = mapping.get(p.ticker)
        if not ticker_info:
            continue
        positions_by_class[ticker_info.asset_class].append(p)

    # Process sells first (overweight classes)
    for cls, adj in adjustment_by_class.items():
        if adj >= 0:
            continue
        sell_amount = abs(adj)
        class_positions = positions_by_class.get(cls, [])
        if not class_positions:
            continue

        # Sort: tax-advantaged first, then within taxable prefer losses
        def _sell_sort_key(p: Position) -> tuple[int, Decimal]:
            is_tax_adv = 0 if p.account_type in TAX_ADVANTAGED else 1
            # For taxable, prefer positions with losses (lower gain = sell first)
            gain = ZERO
            if p.cost_basis_total is not None and p.market_value > 0:
                gain = p.market_value - p.cost_basis_total
            return (is_tax_adv, gain)

        class_positions_sorted = sorted(class_positions, key=_sell_sort_key)

        remaining = sell_amount
        for p in class_positions_sorted:
            if remaining <= 0:
                break
            if p.price <= 0:
                continue

            if p.tax_lots:
                # Lot-aware selling: one trade per lot consumed
                sorted_lots = _sort_lots(p, config)
                lot_strategy = "FIFO"
                if p.account_type not in TAX_ADVANTAGED:
                    lot_strategy = "TLH" if config.tlh_enabled else "HIFO"

                for lot in sorted_lots:
                    if remaining <= 0:
                        break

                    lot_value = lot.shares * p.price
                    sellable_value = min(remaining, lot_value)
                    shares = _quantize_shares(sellable_value / p.price, whole_shares=config.whole_shares_only)
                    if shares <= 0:
                        continue
                    # Don't sell more than the lot has
                    shares = min(shares, lot.shares)
                    actual_value = (shares * p.price).quantize(
                        Decimal("0.01"), rounding=ROUND_HALF_UP
                    )

                    est_gain_loss, trade_warnings = _estimate_gain_loss(
                        p.price, shares, lot.cost_basis_per_share,
                        None, None, p.quantity, p.account_type,
                        config.avoid_gains_in_taxable,
                    )

                    is_loss = (
                        p.account_type == AccountType.TAXABLE
                        and est_gain_loss is not None
                        and est_gain_loss < 0
                    )

                    reasoning = f"Reduce overweight {cls} (lot: {lot.acquisition_date}, {lot_strategy})"
                    if is_loss and config.tlh_enabled:
                        reasoning += f" (TLH opportunity: ~${abs(est_gain_loss)} loss)"

                    trades.append(
                        Trade(
                            account_name=p.account_name,
                            account_type=p.account_type,
                            ticker=p.ticker,
                            action="SELL",
                            shares=shares,
                            estimated_value=actual_value,
                            reasoning=reasoning,
                            warnings=trade_warnings,
                            estimated_gain_loss=est_gain_loss,
                            lot_acquisition_date=lot.acquisition_date,
                        )
                    )
                    pools.add(p.account_name, p.account_type, actual_value)
                    remaining -= actual_value
            else:
                # Blended-basis logic (no lot data)
                sellable_value = min(remaining, p.market_value)
                shares = _quantize_shares(sellable_value / p.price, whole_shares=config.whole_shares_only)
                if shares <= 0:
                    continue
                actual_value = (shares * p.price).quantize(
                    Decimal("0.01"), rounding=ROUND_HALF_UP
                )

                est_gain_loss, trade_warnings = _estimate_gain_loss(
                    p.price, shares, None,
                    p.market_value, p.cost_basis_total, p.quantity,
                    p.account_type, config.avoid_gains_in_taxable,
                )

                is_loss = (
                    p.account_type == AccountType.TAXABLE
                    and est_gain_loss is not None
                    and est_gain_loss < 0
                )

                reasoning = f"Reduce overweight {cls}"
                if is_loss and config.tlh_enabled:
                    reasoning += f" (TLH opportunity: ~${abs(est_gain_loss)} loss)"

                trades.append(
                    Trade(
                        account_name=p.account_name,
                        account_type=p.account_type,
                        ticker=p.ticker,
                        action="SELL",
                        shares=shares,
                        estimated_value=actual_value,
                        reasoning=reasoning,
                        warnings=trade_warnings,
                        estimated_gain_loss=est_gain_loss,
                    )
                )
                pools.add(p.account_name, p.account_type, actual_value)
                remaining -= actual_value

    # Process buys (underweight classes), largest deficit first
    underweight = sorted(
        ((cls, adj) for cls, adj in adjustment_by_class.items() if adj > 0),
        key=lambda x: x[1],
        reverse=True,
    )
    total_shortfall = ZERO
    for cls, buy_amount in underweight:
        buy_trades = _allocate_buys(cls, buy_amount, positions, mapping, config, pools)
        trades.extend(buy_trades)
        bought = sum(t.estimated_value for t in buy_trades)
        shortfall = buy_amount - bought
        if shortfall > config.min_trade_value:
            total_shortfall += shortfall

    if total_shortfall > config.min_trade_value:
        warnings.append(
            f"Insufficient cash to fully rebalance: ${total_shortfall.quantize(Decimal('0.01'), rounding=ROUND_HALF_UP)} shortfall. "
            f"Tax-advantaged accounts can only buy with cash available in that account."
        )

    return trades


def project_positions(
    positions: list[Position],
    trades: list[Trade],
) -> list[Position]:
    """Apply a list of trades to current positions to produce a projected post-trade snapshot.

    BUY trades increase quantity and market value of existing positions, or create a
    synthetic Position for tickers not yet held. SELL trades reduce quantity and market
    value. Positions reduced to zero are excluded from the result.
    """
    projected: dict[tuple[str, str], Position] = {}
    for p in positions:
        projected[(p.account_name, p.ticker)] = p.model_copy(deep=True)

    for trade in trades:
        key = (trade.account_name, trade.ticker)
        if trade.action == "SELL":
            if key in projected:
                p = projected[key]
                new_value = max(ZERO, p.market_value - trade.estimated_value)
                new_qty = max(ZERO, p.quantity - trade.shares)
                projected[key] = p.model_copy(update={"market_value": new_value, "quantity": new_qty})
        elif trade.action == "BUY":
            if key in projected:
                p = projected[key]
                projected[key] = p.model_copy(update={
                    "market_value": p.market_value + trade.estimated_value,
                    "quantity": p.quantity + trade.shares,
                })
            else:
                price = (
                    (trade.estimated_value / trade.shares).quantize(Decimal("0.0001"), rounding=ROUND_HALF_UP)
                    if trade.shares > 0 else ZERO
                )
                projected[key] = Position(
                    account_name=trade.account_name,
                    account_type=trade.account_type,
                    ticker=trade.ticker,
                    description=trade.ticker,
                    quantity=trade.shares,
                    price=price,
                    market_value=trade.estimated_value,
                )

    return [p for p in projected.values() if p.market_value > 0]


def analyze_consolidation(
    positions: list[Position],
    mapping: dict[str, TickerMapping],
) -> ConsolidationAnalysis:
    """Analyze which positions are in end-state (preferred) funds vs legacy funds.

    Returns a ConsolidationAnalysis with per-position opportunities for consolidation.
    """
    end_state_value = ZERO
    legacy_value = ZERO
    opportunities: list[ConsolidationOpportunity] = []

    for p in positions:
        ticker_info = mapping.get(p.ticker)
        if not ticker_info:
            continue
        # Skip cash positions
        if ticker_info.asset_class == "cash":
            continue

        if ticker_info.preferred:
            end_state_value += p.market_value
        else:
            legacy_value += p.market_value

            if ticker_info.consolidate_to:
                # Determine if safe to consolidate
                est_gain_loss: Decimal | None = None
                if p.cost_basis_total is not None and p.quantity > 0:
                    est_gain_loss = p.market_value - p.cost_basis_total

                if p.account_type in TAX_ADVANTAGED:
                    safe = True
                    reason = "Retirement account — no tax cost"
                elif est_gain_loss is not None and est_gain_loss <= 0:
                    safe = True
                    reason = "At a loss — tax-free to consolidate"
                elif est_gain_loss is not None and est_gain_loss > 0:
                    safe = False
                    reason = "At a gain — wait for loss or spending need"
                else:
                    safe = False
                    reason = "No cost basis data"

                opportunities.append(
                    ConsolidationOpportunity(
                        ticker=p.ticker,
                        account_name=p.account_name,
                        account_type=p.account_type,
                        market_value=p.market_value,
                        consolidate_to=ticker_info.consolidate_to,
                        safe_to_consolidate=safe,
                        estimated_gain_loss=est_gain_loss,
                        reason=reason,
                    )
                )

    total = end_state_value + legacy_value
    if total > 0:
        end_state_pct = (end_state_value / total * HUNDRED).quantize(
            Decimal("0.01"), rounding=ROUND_HALF_UP
        )
        legacy_pct = (legacy_value / total * HUNDRED).quantize(
            Decimal("0.01"), rounding=ROUND_HALF_UP
        )
    else:
        end_state_pct = ZERO
        legacy_pct = ZERO

    return ConsolidationAnalysis(
        end_state_value=end_state_value,
        legacy_value=legacy_value,
        end_state_pct=end_state_pct,
        legacy_pct=legacy_pct,
        opportunities=opportunities,
    )


# ---------------------------------------------------------------------------
# Policy-aware engine — new-money-only investing
# ---------------------------------------------------------------------------

def compute_allocation_views(
    positions: list[Position],
    policy: PolicyConfig,
    mapping: dict[str, TickerMapping],
) -> tuple[AllocationView, AllocationView]:
    """Compute total and implementable allocation views.

    Classification rules (source of truth — overrides instrument_type for math):
        stock_classes     = STOCK_ASSET_CLASSES  = {"us_equity", "intl_equity", "reit"}
        defensive_classes = DEFENSIVE_ASSET_CLASSES = {"bonds", "cash"}

    total_view:
        All positions across all accounts.  excluded_value = 0.

    implementable_view:
        Only positions in buy_enabled_account_types.
        excluded_value = sum of positions in IRA/Roth/non-buy-enabled accounts.
        Investable cash in buy-enabled accounts is included.
    """

    def _build_view(
        view_positions: list[Position],
        label: str,
        excluded_value: Decimal,
        excluded_reason: str,
    ) -> AllocationView:
        stock_val = ZERO
        defensive_val = ZERO
        for p in view_positions:
            tm = mapping.get(p.ticker)
            asset_class = tm.asset_class if tm else ""
            if asset_class in STOCK_ASSET_CLASSES:
                stock_val += p.market_value
            elif asset_class in DEFENSIVE_ASSET_CLASSES:
                defensive_val += p.market_value

        total_val = stock_val + defensive_val
        if total_val > ZERO:
            stock_pct = (stock_val / total_val).quantize(
                Decimal("0.0001"), rounding=ROUND_HALF_UP
            )
            def_pct = (defensive_val / total_val).quantize(
                Decimal("0.0001"), rounding=ROUND_HALF_UP
            )
        else:
            stock_pct = ZERO
            def_pct = ZERO

        stock_drift = (stock_pct - policy.target_stock_pct).quantize(
            Decimal("0.0001"), rounding=ROUND_HALF_UP
        )
        within_bands = abs(stock_drift) <= policy.rebalance_band_abs

        return AllocationView(
            label=label,
            total_value=total_val,
            stock_value=stock_val,
            defensive_value=defensive_val,
            stock_pct=stock_pct,
            bond_pct=def_pct,
            target_stock_pct=policy.target_stock_pct,
            target_bond_pct=policy.target_bond_pct,
            stock_drift=stock_drift,
            within_bands=within_bands,
            excluded_value=excluded_value,
            excluded_reason=excluded_reason,
        )

    total_view = _build_view(positions, "Total Portfolio", ZERO, "")

    impl_positions = [
        p for p in positions
        if p.account_type.value in policy.buy_enabled_account_types
    ]
    excluded_val = sum(
        p.market_value
        for p in positions
        if p.account_type.value not in policy.buy_enabled_account_types
    )
    impl_view = _build_view(
        impl_positions,
        "Implementable",
        excluded_val,
        "IRA/Roth/non-buy-enabled accounts",
    )

    return total_view, impl_view


def _build_defensive_instructions(
    defensive_cash_usd: Decimal,
    policy: PolicyConfig,
    account_name: str,
    account_type: AccountType,
) -> list[Trade]:
    """Generate defensive placeholder Trade rows based on defensive_mode."""
    if defensive_cash_usd <= ZERO:
        return []

    def _make(ticker: str, amount: Decimal) -> Trade:
        return Trade(
            account_name=account_name,
            account_type=account_type,
            ticker=ticker,
            action="BUY",
            shares=ZERO,
            estimated_value=amount.quantize(Decimal("0.01"), rounding=ROUND_HALF_UP),
            reasoning="Defensive allocation placeholder — execute manually",
        )

    mode = policy.defensive_mode

    if mode == DefensiveMode.treasury_only:
        return [_make("TREASURY", defensive_cash_usd)]

    if mode == DefensiveMode.treasury_cd_split:
        rows = []
        t_amt = (defensive_cash_usd * policy.treasury_pct).quantize(
            Decimal("0.01"), rounding=ROUND_HALF_UP
        )
        c_amt = (defensive_cash_usd * policy.cd_pct).quantize(
            Decimal("0.01"), rounding=ROUND_HALF_UP
        )
        if t_amt > ZERO:
            rows.append(_make("TREASURY", t_amt))
        if c_amt > ZERO:
            rows.append(_make("CD", c_amt))
        return rows

    # ladder mode
    rungs = policy.ladder_rungs_months
    if not rungs:
        return [_make("TREASURY", defensive_cash_usd)]
    per_rung = (defensive_cash_usd / len(rungs)).quantize(
        Decimal("0.01"), rounding=ROUND_HALF_UP
    )
    return [
        _make(f"TREASURY_{m}M_{policy.ladder_currency}", per_rung)
        for m in rungs
    ]


def new_money_plan(
    positions: list[Position],
    policy: PolicyConfig,
    mapping: dict[str, TickerMapping],
    basket: list[BasketConstituent] | None = None,
    prices: dict[str, Decimal] | None = None,
) -> BuyPlan:
    """New-money-only investment plan under the current policy.

    Never recommends selling existing ETF positions unless allow_legacy_etf_sales=True.
    Band detection is based on the TOTAL portfolio view (strategic risk signal).
    Cash routing/execution uses the implementable view (buy-enabled accounts only).
    """
    from .basket import compute_basket_orders
    from .policy import build_legacy_sell_flags, months_to_reenter_band

    warnings: list[str] = []

    total_view, impl_view = compute_allocation_views(positions, policy, mapping)

    # --- Cash conversion ---
    investable_cash_usd = (policy.investable_cash_eur * policy.eurusd_fx).quantize(
        Decimal("0.01"), rounding=ROUND_HALF_UP
    )
    monthly_cash_usd = (policy.monthly_investable_cash_eur * policy.eurusd_fx).quantize(
        Decimal("0.01"), rounding=ROUND_HALF_UP
    )

    # --- Band detection from TOTAL view (strategic signal) ---
    drift = total_view.stock_drift            # positive = overweight stocks
    band  = policy.rebalance_band_abs
    outside_band = not total_view.within_bands

    # --- Cash split ---
    if drift > band:
        # Overweight stocks: all cash → defensive
        equity_cash_usd    = ZERO
        defensive_cash_usd = investable_cash_usd
    elif drift < -band:
        # Underweight stocks: all cash → equity
        equity_cash_usd    = investable_cash_usd
        defensive_cash_usd = ZERO
    else:
        # Within band: proportional split to nudge toward target
        # Fraction of cash that goes to each side based on relative targets
        total_target = policy.target_stock_pct + policy.target_bond_pct
        if total_target > ZERO:
            eq_frac = (policy.target_stock_pct / total_target).quantize(
                Decimal("0.0001"), rounding=ROUND_HALF_UP
            )
        else:
            eq_frac = Decimal("0.80")
        equity_cash_usd    = (investable_cash_usd * eq_frac).quantize(
            Decimal("0.01"), rounding=ROUND_HALF_UP
        )
        defensive_cash_usd = (investable_cash_usd - equity_cash_usd).quantize(
            Decimal("0.01"), rounding=ROUND_HALF_UP
        )

    # --- Equity instructions ---
    # Route to buy-enabled accounts; use the first buy-enabled account we can find
    impl_positions = [
        p for p in positions
        if p.account_type.value in policy.buy_enabled_account_types
    ]
    # Pick account for trade routing (first buy-enabled position, or taxable default)
    buy_account_name = "Taxable"
    buy_account_type = AccountType.TAXABLE
    for p in impl_positions:
        buy_account_name = p.account_name
        buy_account_type = p.account_type
        break

    equity_instructions: list[Trade] = []
    if equity_cash_usd >= policy.min_trade_value:
        if basket and prices:
            # Build {ticker: Position} for basket holdings in buy-enabled accounts
            basket_tickers = {c.ticker for c in basket}
            current_basket_holdings = {
                p.ticker: p
                for p in impl_positions
                if p.ticker in basket_tickers
            }
            equity_instructions = compute_basket_orders(
                constituents=basket,
                current_holdings=current_basket_holdings,
                equity_cash=equity_cash_usd,
                n_stocks=policy.basket_size,
                min_trade_value=policy.min_trade_value,
                prices=prices,
                account_name=buy_account_name,
                account_type=buy_account_type,
            )
        else:
            # No basket: single placeholder row
            equity_instructions = [
                Trade(
                    account_name=buy_account_name,
                    account_type=buy_account_type,
                    ticker="US_STOCK_BASKET",
                    action="BUY",
                    shares=ZERO,
                    estimated_value=equity_cash_usd,
                    reasoning="Equity allocation placeholder — upload basket CSV to expand",
                )
            ]

    # --- Defensive instructions (from defensive_cash_usd — not equity_cash) ---
    defensive_instructions = _build_defensive_instructions(
        defensive_cash_usd, policy, buy_account_name, buy_account_type
    )

    # --- Horizon / months-to-fix (from total view) ---
    m_fix = months_to_reenter_band(
        stock_value_usd=total_view.stock_value,
        total_value_usd=total_view.total_value,
        target_stock_pct=policy.target_stock_pct,
        band_abs=policy.rebalance_band_abs,
        monthly_new_cash_usd=monthly_cash_usd,
    )
    if monthly_cash_usd <= ZERO:
        warnings.append(
            "monthly_investable_cash_eur is 0 — cannot estimate months to re-enter band. "
            "Set it in Settings for a horizon projection."
        )

    # --- Legacy sell flags ---
    legacy_flags, legacy_trades = build_legacy_sell_flags(
        positions, mapping, policy, outside_band, m_fix
    )

    # Horizon warning
    if (
        outside_band
        and m_fix is not None
        and m_fix > policy.horizon_months
    ):
        warnings.append(
            f"Portfolio is outside target bands and will take ≈{int(m_fix)} months to "
            f"correct with new money alone (>{policy.horizon_months}-month horizon). "
            "Consider enabling allow_legacy_etf_sales."
        )

    from .output import format_why_this_plan
    why = format_why_this_plan(
        total_view=total_view,
        impl_view=impl_view,
        investable_cash_usd=investable_cash_usd,
        equity_cash_usd=equity_cash_usd,
        defensive_cash_usd=defensive_cash_usd,
        policy=policy,
        monthly_cash_usd=monthly_cash_usd,
        months_to_fix=m_fix,
    )

    return BuyPlan(
        total_view=total_view,
        implementable_view=impl_view,
        investable_cash_usd=investable_cash_usd,
        equity_cash_usd=equity_cash_usd,
        defensive_cash_usd=defensive_cash_usd,
        equity_instructions=equity_instructions,
        defensive_instructions=defensive_instructions,
        legacy_sell_flags=legacy_flags,
        legacy_sell_trades=legacy_trades,
        why_text=why,
        warnings=warnings,
        months_to_reenter_band=m_fix,
    )
