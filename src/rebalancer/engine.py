from dataclasses import dataclass, field
from decimal import ROUND_HALF_UP, Decimal

from .models import (
    AccountType,
    AllocationTarget,
    Position,
    RebalanceConfig,
    RebalanceResult,
    TaxImpact,
    TickerMapping,
    Trade,
)
from .tlh import check_wash_sales, find_tlh_opportunities

TAX_ADVANTAGED = {
    AccountType.TRADITIONAL_IRA,
    AccountType.ROTH_IRA,
    AccountType.ROTH_401K,
    AccountType.FOUR_01K,
    AccountType.HSA,
}


@dataclass
class CashPools:
    """Tracks available cash per funding boundary.

    Taxable accounts share one pool (money can move between them).
    Each tax-advantaged account is isolated.
    """

    taxable_pool: Decimal = Decimal("0")
    tax_adv_pools: dict[str, Decimal] = field(default_factory=dict)

    def available(self, account_name: str, account_type: AccountType) -> Decimal:
        if account_type in TAX_ADVANTAGED:
            return self.tax_adv_pools.get(account_name, Decimal("0"))
        return self.taxable_pool

    def add(self, account_name: str, account_type: AccountType, amount: Decimal) -> None:
        if account_type in TAX_ADVANTAGED:
            self.tax_adv_pools[account_name] = (
                self.tax_adv_pools.get(account_name, Decimal("0")) + amount
            )
        else:
            self.taxable_pool += amount

    def spend(self, account_name: str, account_type: AccountType, amount: Decimal) -> None:
        if account_type in TAX_ADVANTAGED:
            self.tax_adv_pools[account_name] = (
                self.tax_adv_pools.get(account_name, Decimal("0")) - amount
            )
        else:
            self.taxable_pool -= amount


def _quantize_pct(value: Decimal) -> Decimal:
    return value.quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)


def _quantize_shares(value: Decimal) -> Decimal:
    return value.quantize(Decimal("0.001"), rounding=ROUND_HALF_UP)


def rebalance(
    positions: list[Position],
    targets: list[AllocationTarget],
    mapping: dict[str, TickerMapping],
    config: RebalanceConfig,
) -> RebalanceResult:
    """Run the full rebalancing algorithm."""
    warnings: list[str] = []

    # Total portfolio value
    total_value = sum(p.market_value for p in positions)
    if total_value == 0:
        return RebalanceResult(
            total_portfolio_value=Decimal("0"),
            current_allocation={},
            target_allocation={t.asset_class: t.target_pct for t in targets},
            drift={},
            trades=[],
            warnings=["Portfolio has no value."],
        )

    effective_total = total_value + config.cash_to_invest

    # Current allocation by asset class
    value_by_class: dict[str, Decimal] = {}
    for p in positions:
        ticker_info = mapping.get(p.ticker)
        asset_class = ticker_info.asset_class if ticker_info else "unmapped"
        value_by_class[asset_class] = (
            value_by_class.get(asset_class, Decimal("0")) + p.market_value
        )

    if "unmapped" in value_by_class:
        unmapped_tickers = {
            p.ticker for p in positions if p.ticker not in mapping
        }
        warnings.append(
            f"Unmapped tickers (excluded from rebalancing): {', '.join(sorted(unmapped_tickers))}"
        )

    current_allocation: dict[str, Decimal] = {}
    for cls, val in value_by_class.items():
        current_allocation[cls] = _quantize_pct(val / total_value * Decimal("100"))

    target_allocation: dict[str, Decimal] = {
        t.asset_class: t.target_pct for t in targets
    }

    # Drift: positive = overweight, negative = underweight
    all_classes = sorted(set(current_allocation.keys()) | set(target_allocation.keys()))
    drift: dict[str, Decimal] = {}
    for cls in all_classes:
        current = current_allocation.get(cls, Decimal("0"))
        target = target_allocation.get(cls, Decimal("0"))
        drift[cls] = _quantize_pct(current - target)

    # Calculate target dollar amounts based on effective total
    target_amounts: dict[str, Decimal] = {}
    for cls in all_classes:
        target_pct = target_allocation.get(cls, Decimal("0"))
        target_amounts[cls] = effective_total * target_pct / Decimal("100")

    # Dollar adjustment needed per class (negative = need to sell, positive = need to buy)
    adjustment_by_class: dict[str, Decimal] = {}
    for cls in all_classes:
        if cls == "unmapped":
            continue
        current_val = value_by_class.get(cls, Decimal("0"))
        target_val = target_amounts.get(cls, Decimal("0"))
        adj = target_val - current_val
        # Skip if drift below threshold
        if abs(drift.get(cls, Decimal("0"))) < config.threshold_pct and config.cash_to_invest == 0:
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
        wash_warnings = check_wash_sales(trades, mapping)
        for tw in wash_warnings:
            warnings.append(tw)

    # Compute tax impact summary (only taxable sell trades matter)
    total_gains = Decimal("0")
    total_losses = Decimal("0")
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

    return RebalanceResult(
        total_portfolio_value=total_value,
        current_allocation=current_allocation,
        target_allocation=target_allocation,
        drift=drift,
        trades=trades,
        warnings=warnings,
        tax_impact=tax_impact,
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
            shares = _quantize_shares(buy_amount / price)
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
                return ticker, Decimal("0"), None
        return None, Decimal("0"), None

    # Prefer tax-advantaged accounts
    tax_adv = [c for c in candidates if c.account_type in TAX_ADVANTAGED]
    if tax_adv:
        best = tax_adv[0]
    else:
        best = candidates[0]

    return best.ticker, best.price, best


def _build_initial_cash_pools(
    positions: list[Position],
    mapping: dict[str, TickerMapping],
) -> CashPools:
    """Build initial cash pools from cash positions."""
    pools = CashPools()
    for p in positions:
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

    # Reference ticker = largest existing position in this class (for new-position buys)
    ref_ticker: str = class_positions[0].ticker
    ref_price: Decimal = class_positions[0].price
    best_ref_value = class_positions[0].market_value
    for p in class_positions[1:]:
        if p.market_value > best_ref_value:
            ref_ticker = p.ticker
            ref_price = p.price
            best_ref_value = p.market_value

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
        all_candidates.append((name, p.account_type, p.ticker, p.price, False))

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
        shares = _quantize_shares(buy_here / price)
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
    positions_by_class: dict[str, list[Position]] = {}
    for p in positions:
        ticker_info = mapping.get(p.ticker)
        if not ticker_info:
            continue
        cls = ticker_info.asset_class
        positions_by_class.setdefault(cls, []).append(p)

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
            gain = Decimal("0")
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

            sellable_value = min(remaining, p.market_value)
            shares = _quantize_shares(sellable_value / p.price)
            if shares <= 0:
                continue
            actual_value = (shares * p.price).quantize(
                Decimal("0.01"), rounding=ROUND_HALF_UP
            )

            trade_warnings: list[str] = []
            est_gain_loss: Decimal | None = None

            # Compute per-share gain/loss if we have cost basis
            if p.cost_basis_total is not None and p.quantity > 0:
                gain_per_share = (p.market_value - p.cost_basis_total) / p.quantity
                est_gain_loss = (gain_per_share * shares).quantize(
                    Decimal("0.01"), rounding=ROUND_HALF_UP
                )

            # Check for taxable gain
            if (
                p.account_type == AccountType.TAXABLE
                and config.avoid_gains_in_taxable
                and est_gain_loss is not None
                and est_gain_loss > 0
            ):
                trade_warnings.append(
                    f"Selling at estimated gain of ${est_gain_loss} in taxable account"
                )

            # Check for TLH opportunity
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
            # Credit sell proceeds to the appropriate cash pool
            pools.add(p.account_name, p.account_type, actual_value)
            remaining -= actual_value

    # Process buys (underweight classes), largest deficit first
    underweight = sorted(
        ((cls, adj) for cls, adj in adjustment_by_class.items() if adj > 0),
        key=lambda x: x[1],
        reverse=True,
    )
    total_shortfall = Decimal("0")
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
