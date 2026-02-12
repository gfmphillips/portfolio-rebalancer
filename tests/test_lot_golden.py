"""Hand-verified golden tests and invariant checks for tax lot-aware selling.

Every expected value in these tests was computed by hand on paper and
cross-checked.  If any assertion fails, the financial math is wrong.
"""

from decimal import Decimal

import pytest

from rebalancer.engine import rebalance
from rebalancer.models import (
    AccountType,
    AllocationTarget,
    Position,
    RebalanceConfig,
    TaxLot,
    TickerMapping,
    Trade,
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _pos(account, acct_type, ticker, qty, price, cost_basis=None, lots=None):
    return Position(
        account_name=account,
        account_type=acct_type,
        ticker=ticker,
        description=ticker,
        quantity=Decimal(str(qty)),
        price=Decimal(str(price)),
        market_value=Decimal(str(qty)) * Decimal(str(price)),
        cost_basis_total=Decimal(str(cost_basis)) if cost_basis is not None else None,
        tax_lots=lots or [],
    )


def _cash_pos(account, acct_type, value):
    return Position(
        account_name=account,
        account_type=acct_type,
        ticker="SPAXX",
        description="Cash",
        quantity=Decimal("0"),
        price=Decimal("0"),
        market_value=Decimal(str(value)),
    )


def _lot(date, shares, cost):
    return TaxLot(
        acquisition_date=date,
        shares=Decimal(str(shares)),
        cost_basis_per_share=Decimal(str(cost)),
    )


MAPPING = {
    "VTI": TickerMapping(asset_class="us_equity"),
    "BND": TickerMapping(asset_class="bonds"),
    "SPAXX": TickerMapping(asset_class="cash"),
}


# ===================================================================
# GOLDEN TEST 1 — HIFO in a taxable account
# ===================================================================
#
# Portfolio ($20,000 total):
#   Taxable VTI: 60 shares × $200 = $12,000  (us_equity 60%)
#     Lot A: 30 sh @ $150  (acquired 2020-03-15)
#     Lot B: 30 sh @ $250  (acquired 2021-06-01)
#   Taxable BND: 40 shares × $100 = $4,000   (bonds 20%)
#   Taxable SPAXX: $4,000                     (cash 20%)
#
# Target: us_equity 40%, bonds 40%, cash 20%
#
# Adjustments (effective_total = $20,000, cash_to_invest = 0):
#   us_equity: target $8,000, current $12,000 → sell $4,000
#   bonds:     target $8,000, current $4,000  → buy  $4,000
#   cash:      target $4,000, current $4,000  → no change
#
# HIFO sell order: Lot B ($250 cost, highest) first.
#   Need $4,000 → $4,000 / $200 = 20.000 shares from Lot B.
#   Lot B has 30 shares, so partial consumption (20 of 30).
#   Gain/loss: ($200 − $250) × 20 = −$50 × 20 = −$1,000.00
#
# Buy: BND $4,000 / $100 = 40.000 shares
#
# Expected trades:
#   1. SELL VTI  20.000 sh  $4,000.00  lot=2021-06-01  G/L=−$1,000.00
#   2. BUY  BND  40.000 sh  $4,000.00
# ===================================================================

class TestGoldenHIFO:
    @pytest.fixture
    def result(self):
        positions = [
            _pos("Taxable", AccountType.TAXABLE, "VTI", 60, 200, cost_basis=12000, lots=[
                _lot("2020-03-15", 30, 150),
                _lot("2021-06-01", 30, 250),
            ]),
            _pos("Taxable", AccountType.TAXABLE, "BND", 40, 100, cost_basis=4000),
            _cash_pos("Taxable", AccountType.TAXABLE, 4000),
        ]
        targets = [
            AllocationTarget(asset_class="us_equity", target_pct=Decimal("40")),
            AllocationTarget(asset_class="bonds", target_pct=Decimal("40")),
            AllocationTarget(asset_class="cash", target_pct=Decimal("20")),
        ]
        config = RebalanceConfig(
            threshold_pct=Decimal("3.0"),
            min_trade_value=Decimal("50"),
            tlh_enabled=False,
            avoid_gains_in_taxable=False,
        )
        return rebalance(positions, targets, MAPPING, config)

    def test_one_sell_trade(self, result):
        sells = [t for t in result.trades if t.action == "SELL"]
        assert len(sells) == 1

    def test_sell_ticker_and_shares(self, result):
        sell = [t for t in result.trades if t.action == "SELL"][0]
        assert sell.ticker == "VTI"
        assert sell.shares == Decimal("20.000")

    def test_sell_value(self, result):
        sell = [t for t in result.trades if t.action == "SELL"][0]
        assert sell.estimated_value == Decimal("4000.00")

    def test_sell_lot_date(self, result):
        sell = [t for t in result.trades if t.action == "SELL"][0]
        # HIFO: highest cost lot ($250, acquired 2021-06-01) sold first
        assert sell.lot_acquisition_date == "2021-06-01"

    def test_sell_gain_loss(self, result):
        sell = [t for t in result.trades if t.action == "SELL"][0]
        # (price − cost) × shares = ($200 − $250) × 20 = −$1,000
        assert sell.estimated_gain_loss == Decimal("-1000.00")

    def test_buy_trade(self, result):
        buys = [t for t in result.trades if t.action == "BUY"]
        assert len(buys) == 1
        buy = buys[0]
        assert buy.ticker == "BND"
        assert buy.shares == Decimal("40.000")
        assert buy.estimated_value == Decimal("4000.00")

    def test_tax_impact(self, result):
        ti = result.tax_impact
        assert ti.estimated_total_losses == Decimal("-1000.00")
        assert ti.estimated_total_gains == Decimal("0")
        assert ti.estimated_net == Decimal("-1000.00")
        assert ti.taxable_trades_count == 1


# ===================================================================
# GOLDEN TEST 2 — FIFO in a Roth IRA
# ===================================================================
#
# Portfolio ($20,000 total, all in Roth IRA):
#   VTI: 60 shares × $200 = $12,000  (us_equity 60%)
#     Lot A: 20 sh @ $150  (acquired 2020-01-15) ← oldest
#     Lot B: 20 sh @ $180  (acquired 2021-03-01)
#     Lot C: 20 sh @ $250  (acquired 2022-06-01) ← newest
#   BND: 40 shares × $100 = $4,000   (bonds 20%)
#   SPAXX: $4,000                     (cash 20%)
#
# Target: us_equity 40%, bonds 40%, cash 20%
# Sell $4,000 of VTI.
#
# FIFO: Lot A (2020-01-15, oldest) first.
#   $4,000 / $200 = 20.000 shares.  Lot A has exactly 20 → full lot consumed.
#   Gain/loss: ($200 − $150) × 20 = +$1,000.00  (doesn't matter, Roth)
#
# Expected trades:
#   1. SELL VTI  20.000 sh  $4,000.00  lot=2020-01-15  G/L=+$1,000.00
#   2. BUY  BND  40.000 sh  $4,000.00
# ===================================================================

class TestGoldenFIFO:
    @pytest.fixture
    def result(self):
        positions = [
            _pos("Roth IRA", AccountType.ROTH_IRA, "VTI", 60, 200, cost_basis=11600, lots=[
                _lot("2020-01-15", 20, 150),
                _lot("2021-03-01", 20, 180),
                _lot("2022-06-01", 20, 250),
            ]),
            _pos("Roth IRA", AccountType.ROTH_IRA, "BND", 40, 100, cost_basis=4000),
            _cash_pos("Roth IRA", AccountType.ROTH_IRA, 4000),
        ]
        targets = [
            AllocationTarget(asset_class="us_equity", target_pct=Decimal("40")),
            AllocationTarget(asset_class="bonds", target_pct=Decimal("40")),
            AllocationTarget(asset_class="cash", target_pct=Decimal("20")),
        ]
        config = RebalanceConfig(
            threshold_pct=Decimal("3.0"),
            min_trade_value=Decimal("50"),
            tlh_enabled=True,  # shouldn't matter — Roth uses FIFO regardless
        )
        return rebalance(positions, targets, MAPPING, config)

    def test_sell_lot_is_oldest(self, result):
        sell = [t for t in result.trades if t.action == "SELL"][0]
        assert sell.lot_acquisition_date == "2020-01-15"

    def test_sell_shares_and_value(self, result):
        sell = [t for t in result.trades if t.action == "SELL"][0]
        assert sell.shares == Decimal("20.000")
        assert sell.estimated_value == Decimal("4000.00")

    def test_sell_gain_loss(self, result):
        sell = [t for t in result.trades if t.action == "SELL"][0]
        # ($200 − $150) × 20 = +$1,000
        assert sell.estimated_gain_loss == Decimal("1000.00")

    def test_fifo_label_in_reasoning(self, result):
        sell = [t for t in result.trades if t.action == "SELL"][0]
        assert "FIFO" in sell.reasoning

    def test_no_taxable_impact(self, result):
        # Roth IRA sells shouldn't appear in taxable impact
        ti = result.tax_impact
        assert ti.taxable_trades_count == 0


# ===================================================================
# GOLDEN TEST 3 — TLH maximises loss (not gain)
# ===================================================================
#
# This test proves TLH correctness.  Price = $200.
#   Lot A: 30 sh @ $150 → per-share gain = +$50
#   Lot B: 30 sh @ $250 → per-share loss = −$50
#
# TLH must sell Lot B first (highest cost → biggest loss).
# Selling Lot A first would realise a GAIN — the opposite of TLH.
#
# Sell $4,000 → 20 shares from Lot B.
# G/L: ($200 − $250) × 20 = −$1,000
# ===================================================================

class TestGoldenTLH:
    @pytest.fixture
    def result(self):
        positions = [
            _pos("Taxable", AccountType.TAXABLE, "VTI", 60, 200, cost_basis=12000, lots=[
                _lot("2020-01-01", 30, 150),   # gain lot
                _lot("2021-01-01", 30, 250),    # loss lot
            ]),
            _pos("Taxable", AccountType.TAXABLE, "BND", 40, 100, cost_basis=4000),
            _cash_pos("Taxable", AccountType.TAXABLE, 4000),
        ]
        targets = [
            AllocationTarget(asset_class="us_equity", target_pct=Decimal("40")),
            AllocationTarget(asset_class="bonds", target_pct=Decimal("40")),
            AllocationTarget(asset_class="cash", target_pct=Decimal("20")),
        ]
        config = RebalanceConfig(
            threshold_pct=Decimal("3.0"),
            min_trade_value=Decimal("50"),
            tlh_enabled=True,
            avoid_gains_in_taxable=False,
        )
        return rebalance(positions, targets, MAPPING, config)

    def test_tlh_sells_loss_lot_first(self, result):
        sell = [t for t in result.trades if t.action == "SELL"][0]
        # Must be the $250-cost lot (loss), not the $150-cost lot (gain)
        assert sell.lot_acquisition_date == "2021-01-01"

    def test_tlh_gain_loss_is_negative(self, result):
        sell = [t for t in result.trades if t.action == "SELL"][0]
        # ($200 − $250) × 20 = −$1,000
        assert sell.estimated_gain_loss == Decimal("-1000.00")

    def test_tlh_label_in_reasoning(self, result):
        sell = [t for t in result.trades if t.action == "SELL"][0]
        assert "TLH" in sell.reasoning


# ===================================================================
# GOLDEN TEST 4 — Multiple lots consumed to fill sell amount
# ===================================================================
#
# Portfolio ($30,000 total):
#   Taxable VTI: 100 shares × $200 = $20,000  (us_equity 66.7%)
#     Lot A: 20 sh @ $180  (acquired 2020-01-01)
#     Lot B: 30 sh @ $220  (acquired 2021-01-01)
#     Lot C: 50 sh @ $160  (acquired 2022-01-01)
#   Taxable BND: 50 shares × $100 = $5,000    (bonds 16.7%)
#   Taxable SPAXX: $5,000                      (cash 16.7%)
#
# Target: us_equity 40%, bonds 40%, cash 20%
# us_equity target: $12,000, current $20,000 → sell $8,000
#
# HIFO order: Lot B ($220), Lot A ($180), Lot C ($160)
#   Lot B: 30 shares × $200 = $6,000.  Need $8,000 → consume all 30.
#     G/L: ($200 − $220) × 30 = −$600
#   Remaining: $8,000 − $6,000 = $2,000
#   Lot A: 20 shares × $200 = $4,000 > $2,000 needed.
#     Sell $2,000 / $200 = 10 shares from Lot A.
#     G/L: ($200 − $180) × 10 = +$200
#
# Expected sells:
#   1. SELL VTI 30.000 sh $6,000 lot=2021-01-01 G/L=−$600
#   2. SELL VTI 10.000 sh $2,000 lot=2020-01-01 G/L=+$200
# ===================================================================

class TestGoldenMultipleLots:
    @pytest.fixture
    def result(self):
        positions = [
            _pos("Taxable", AccountType.TAXABLE, "VTI", 100, 200, cost_basis=18400, lots=[
                _lot("2020-01-01", 20, 180),
                _lot("2021-01-01", 30, 220),
                _lot("2022-01-01", 50, 160),
            ]),
            _pos("Taxable", AccountType.TAXABLE, "BND", 50, 100, cost_basis=5000),
            _cash_pos("Taxable", AccountType.TAXABLE, 5000),
        ]
        targets = [
            AllocationTarget(asset_class="us_equity", target_pct=Decimal("40")),
            AllocationTarget(asset_class="bonds", target_pct=Decimal("40")),
            AllocationTarget(asset_class="cash", target_pct=Decimal("20")),
        ]
        config = RebalanceConfig(
            threshold_pct=Decimal("3.0"),
            min_trade_value=Decimal("50"),
            tlh_enabled=False,
            avoid_gains_in_taxable=False,
        )
        return rebalance(positions, targets, MAPPING, config)

    def test_two_sell_trades(self, result):
        sells = [t for t in result.trades if t.action == "SELL"]
        assert len(sells) == 2

    def test_first_sell_is_highest_cost_lot(self, result):
        sells = [t for t in result.trades if t.action == "SELL"]
        assert sells[0].lot_acquisition_date == "2021-01-01"
        assert sells[0].shares == Decimal("30.000")
        assert sells[0].estimated_value == Decimal("6000.00")
        assert sells[0].estimated_gain_loss == Decimal("-600.00")

    def test_second_sell_is_partial_next_lot(self, result):
        sells = [t for t in result.trades if t.action == "SELL"]
        assert sells[1].lot_acquisition_date == "2020-01-01"
        assert sells[1].shares == Decimal("10.000")
        assert sells[1].estimated_value == Decimal("2000.00")
        assert sells[1].estimated_gain_loss == Decimal("200.00")

    def test_total_sell_value(self, result):
        sells = [t for t in result.trades if t.action == "SELL"]
        total = sum(t.estimated_value for t in sells)
        assert total == Decimal("8000.00")

    def test_tax_impact_net(self, result):
        ti = result.tax_impact
        # −$600 + $200 = −$400 net
        assert ti.estimated_net == Decimal("-400.00")
        assert ti.estimated_total_losses == Decimal("-600.00")
        assert ti.estimated_total_gains == Decimal("200.00")


# ===================================================================
# GOLDEN TEST 5 — No lots → blended basis (backward compat)
# ===================================================================
#
# Same portfolio shape but without lots.
# VTI: 60 sh × $200 = $12,000, cost_basis_total = $10,800
# Blended cost/share = $10,800 / 60 = $180/share
# Sell $4,000 → 20 shares
# Blended G/L = ($12,000 − $10,800) / 60 × 20 = $20 × 20 = $400
# ===================================================================

class TestGoldenBlendedBasis:
    @pytest.fixture
    def result(self):
        positions = [
            _pos("Taxable", AccountType.TAXABLE, "VTI", 60, 200, cost_basis=10800),
            _pos("Taxable", AccountType.TAXABLE, "BND", 40, 100, cost_basis=4000),
            _cash_pos("Taxable", AccountType.TAXABLE, 4000),
        ]
        targets = [
            AllocationTarget(asset_class="us_equity", target_pct=Decimal("40")),
            AllocationTarget(asset_class="bonds", target_pct=Decimal("40")),
            AllocationTarget(asset_class="cash", target_pct=Decimal("20")),
        ]
        config = RebalanceConfig(
            threshold_pct=Decimal("3.0"),
            min_trade_value=Decimal("50"),
            tlh_enabled=False,
            avoid_gains_in_taxable=False,
        )
        return rebalance(positions, targets, MAPPING, config)

    def test_no_lot_date(self, result):
        sell = [t for t in result.trades if t.action == "SELL"][0]
        assert sell.lot_acquisition_date is None

    def test_blended_gain_loss(self, result):
        sell = [t for t in result.trades if t.action == "SELL"][0]
        # Blended: gain_per_share = (12000 − 10800) / 60 = 20
        # 20 shares × $20 = $400
        assert sell.estimated_gain_loss == Decimal("400.00")


# ===================================================================
# INVARIANT CHECKS
# ===================================================================
# These run the engine with various inputs and verify properties that
# must ALWAYS hold, regardless of the specific numbers.
# ===================================================================

class TestInvariants:
    """Properties that must hold for any lot-aware rebalance run."""

    def _run(self, lots, acct_type=AccountType.TAXABLE, tlh=False):
        """Run a rebalance with the given lots on VTI (overweight)."""
        total_lot_shares = sum(lot.shares for lot in lots)
        positions = [
            _pos("Acct", acct_type, "VTI", total_lot_shares, 200,
                 cost_basis=None, lots=lots),
            _pos("Acct", acct_type, "BND", 20, 100, cost_basis=2000),
            _cash_pos("Acct", acct_type, 2000),
        ]
        total = sum(p.market_value for p in positions)
        targets = [
            AllocationTarget(asset_class="us_equity", target_pct=Decimal("40")),
            AllocationTarget(asset_class="bonds", target_pct=Decimal("40")),
            AllocationTarget(asset_class="cash", target_pct=Decimal("20")),
        ]
        config = RebalanceConfig(
            threshold_pct=Decimal("3.0"),
            min_trade_value=Decimal("50"),
            tlh_enabled=tlh,
            avoid_gains_in_taxable=False,
        )
        return rebalance(positions, targets, MAPPING, config)

    # --- Invariant 1: shares sold from a lot never exceed lot size ---

    @pytest.mark.parametrize("lots", [
        [_lot("2020-01-01", 10, 150), _lot("2021-01-01", 10, 250)],
        [_lot("2020-01-01", 5, 100), _lot("2021-01-01", 5, 300), _lot("2022-01-01", 5, 200)],
        [_lot("2020-01-01", 100, 180)],  # single large lot
        [_lot("2020-01-01", 3, 150), _lot("2021-01-01", 3, 250), _lot("2022-01-01", 3, 200)],  # small lots
    ])
    def test_shares_sold_never_exceed_lot_size(self, lots):
        result = self._run(lots)
        lot_share_map = {lot.acquisition_date: lot.shares for lot in lots}
        for t in result.trades:
            if t.action == "SELL" and t.lot_acquisition_date is not None:
                assert t.shares <= lot_share_map[t.lot_acquisition_date], (
                    f"Sold {t.shares} from lot {t.lot_acquisition_date} "
                    f"which only has {lot_share_map[t.lot_acquisition_date]} shares"
                )

    # --- Invariant 2: total shares sold never exceed position quantity ---

    @pytest.mark.parametrize("lots", [
        [_lot("2020-01-01", 10, 150), _lot("2021-01-01", 10, 250)],
        [_lot("2020-01-01", 50, 180)],
        [_lot("2020-01-01", 5, 100), _lot("2021-01-01", 5, 200), _lot("2022-01-01", 5, 300)],
    ])
    def test_total_shares_sold_never_exceed_position(self, lots):
        total_position_shares = sum(lot.shares for lot in lots)
        result = self._run(lots)
        vti_sells = [t for t in result.trades if t.action == "SELL" and t.ticker == "VTI"]
        total_sold = sum(t.shares for t in vti_sells)
        assert total_sold <= total_position_shares

    # --- Invariant 3: per-lot gain/loss = (price − cost) × shares ---

    @pytest.mark.parametrize("lots", [
        [_lot("2020-01-01", 20, 150), _lot("2021-01-01", 20, 250)],
        [_lot("2020-01-01", 30, 100), _lot("2021-01-01", 30, 300)],
    ])
    def test_gain_loss_matches_lot_math(self, lots):
        result = self._run(lots)
        price = Decimal("200")  # VTI price in _run
        lot_cost_map = {lot.acquisition_date: lot.cost_basis_per_share for lot in lots}
        for t in result.trades:
            if t.action == "SELL" and t.lot_acquisition_date is not None:
                expected = ((price - lot_cost_map[t.lot_acquisition_date]) * t.shares).quantize(Decimal("0.01"))
                assert t.estimated_gain_loss == expected, (
                    f"Lot {t.lot_acquisition_date}: expected G/L {expected}, got {t.estimated_gain_loss}"
                )

    # --- Invariant 4: HIFO order — each subsequent lot has equal or lower cost ---

    def test_hifo_order_descending_cost(self):
        lots = [
            _lot("2020-01-01", 10, 150),
            _lot("2021-01-01", 10, 300),
            _lot("2022-01-01", 10, 200),
        ]
        result = self._run(lots, acct_type=AccountType.TAXABLE, tlh=False)
        lot_sells = [t for t in result.trades if t.action == "SELL" and t.lot_acquisition_date is not None]
        lot_cost_map = {lot.acquisition_date: lot.cost_basis_per_share for lot in lots}
        costs = [lot_cost_map[t.lot_acquisition_date] for t in lot_sells]
        for i in range(len(costs) - 1):
            assert costs[i] >= costs[i + 1], (
                f"HIFO violated: lot {lot_sells[i].lot_acquisition_date} cost {costs[i]} "
                f"followed by lot {lot_sells[i+1].lot_acquisition_date} cost {costs[i+1]}"
            )

    # --- Invariant 5: FIFO order — each subsequent lot has equal or later date ---

    def test_fifo_order_ascending_date(self):
        lots = [
            _lot("2022-06-01", 10, 200),
            _lot("2020-01-15", 10, 150),
            _lot("2021-03-01", 10, 180),
        ]
        result = self._run(lots, acct_type=AccountType.ROTH_IRA)
        lot_sells = [t for t in result.trades if t.action == "SELL" and t.lot_acquisition_date is not None]
        dates = [t.lot_acquisition_date for t in lot_sells]
        for i in range(len(dates) - 1):
            assert dates[i] <= dates[i + 1], (
                f"FIFO violated: lot {dates[i]} followed by {dates[i+1]}"
            )

    # --- Invariant 6: sell proceeds match value ---

    @pytest.mark.parametrize("lots", [
        [_lot("2020-01-01", 20, 150), _lot("2021-01-01", 20, 250)],
        [_lot("2020-01-01", 50, 180)],
    ])
    def test_sell_value_equals_shares_times_price(self, lots):
        result = self._run(lots)
        price = Decimal("200")
        for t in result.trades:
            if t.action == "SELL" and t.lot_acquisition_date is not None:
                expected = (t.shares * price).quantize(Decimal("0.01"))
                assert t.estimated_value == expected

    # --- Invariant 7: tax impact totals match individual trades ---

    @pytest.mark.parametrize("lots,acct_type", [
        ([_lot("2020-01-01", 20, 150), _lot("2021-01-01", 20, 250)], AccountType.TAXABLE),
        ([_lot("2020-01-01", 20, 150), _lot("2021-01-01", 20, 250)], AccountType.ROTH_IRA),
    ])
    def test_tax_impact_matches_trade_sum(self, lots, acct_type):
        result = self._run(lots, acct_type=acct_type)
        taxable_sells = [
            t for t in result.trades
            if t.action == "SELL"
            and t.account_type == AccountType.TAXABLE
            and t.estimated_gain_loss is not None
        ]
        expected_gains = sum((t.estimated_gain_loss for t in taxable_sells if t.estimated_gain_loss > 0), Decimal("0"))
        expected_losses = sum((t.estimated_gain_loss for t in taxable_sells if t.estimated_gain_loss < 0), Decimal("0"))
        ti = result.tax_impact
        assert ti.estimated_total_gains == expected_gains
        assert ti.estimated_total_losses == expected_losses
        assert ti.estimated_net == (expected_gains + expected_losses).quantize(Decimal("0.01"))
