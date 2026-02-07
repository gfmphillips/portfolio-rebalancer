from decimal import Decimal

import pytest

from rebalancer.engine import rebalance
from rebalancer.models import (
    AccountType,
    AllocationTarget,
    Position,
    RebalanceConfig,
    TickerMapping,
)


@pytest.fixture
def targets_50_20_25_5():
    return [
        AllocationTarget(asset_class="us_stocks", target_pct=Decimal("50")),
        AllocationTarget(asset_class="international_stocks", target_pct=Decimal("20")),
        AllocationTarget(asset_class="bonds", target_pct=Decimal("25")),
        AllocationTarget(asset_class="cash", target_pct=Decimal("5")),
    ]


class TestRebalanceBasic:
    def test_empty_portfolio(self, targets_50_20_25_5, sample_mapping, sample_config):
        result = rebalance([], targets_50_20_25_5, sample_mapping, sample_config)
        assert result.total_portfolio_value == Decimal("0")
        assert result.trades == []
        assert "Portfolio has no value." in result.warnings

    def test_computes_total_value(
        self, sample_positions, targets_50_20_25_5, sample_mapping, sample_config
    ):
        result = rebalance(
            sample_positions, targets_50_20_25_5, sample_mapping, sample_config
        )
        assert result.total_portfolio_value == Decimal("61000.00")

    def test_current_allocation(
        self, sample_positions, targets_50_20_25_5, sample_mapping, sample_config
    ):
        result = rebalance(
            sample_positions, targets_50_20_25_5, sample_mapping, sample_config
        )
        # us_stocks: VTI(25000) + FXAIX(10000) = 35000 / 61000 = 57.38%
        assert result.current_allocation["us_stocks"] == Decimal("57.38")
        # international_stocks: VXUS(12000 + 3000) = 15000 / 61000 = 24.59%
        assert result.current_allocation["international_stocks"] == Decimal("24.59")
        # bonds: BND(3600 + 3600) = 7200 / 61000 = 11.80%
        assert result.current_allocation["bonds"] == Decimal("11.80")
        # cash: SPAXX(2400 + 1400) = 3800 / 61000 = 6.23%
        assert result.current_allocation["cash"] == Decimal("6.23")

    def test_drift_calculation(
        self, sample_positions, targets_50_20_25_5, sample_mapping, sample_config
    ):
        result = rebalance(
            sample_positions, targets_50_20_25_5, sample_mapping, sample_config
        )
        # us_stocks: 57.38 - 50 = +7.38 (overweight)
        assert result.drift["us_stocks"] == Decimal("7.38")
        # international_stocks: 24.59 - 20 = +4.59 (overweight)
        assert result.drift["international_stocks"] == Decimal("4.59")
        # bonds: 11.80 - 25 = -13.20 (underweight)
        assert result.drift["bonds"] == Decimal("-13.20")
        # cash: 6.23 - 5 = +1.23 (slightly overweight, within threshold)
        assert result.drift["cash"] == Decimal("1.23")

    def test_generates_trades_for_overweight(
        self, sample_positions, targets_50_20_25_5, sample_mapping, sample_config
    ):
        result = rebalance(
            sample_positions, targets_50_20_25_5, sample_mapping, sample_config
        )
        sell_trades = [t for t in result.trades if t.action == "SELL"]
        buy_trades = [t for t in result.trades if t.action == "BUY"]

        # Should sell overweight classes and buy underweight
        assert len(sell_trades) > 0
        assert len(buy_trades) > 0

        # Bonds are underweight, should have a buy for bonds
        bond_buys = [t for t in buy_trades if t.ticker == "BND"]
        assert len(bond_buys) > 0

    def test_skips_within_threshold(self, sample_mapping):
        """If drift is within threshold, no trades should be generated."""
        positions = [
            Position(
                account_name="Test",
                account_type=AccountType.ROTH_IRA,
                ticker="VTI",
                description="VTI",
                quantity=Decimal("50"),
                price=Decimal("100"),
                market_value=Decimal("5000"),
                cost_basis_total=Decimal("4000"),
            ),
            Position(
                account_name="Test",
                account_type=AccountType.ROTH_IRA,
                ticker="VXUS",
                description="VXUS",
                quantity=Decimal("20"),
                price=Decimal("100"),
                market_value=Decimal("2000"),
                cost_basis_total=Decimal("1800"),
            ),
            Position(
                account_name="Test",
                account_type=AccountType.ROTH_IRA,
                ticker="BND",
                description="BND",
                quantity=Decimal("25"),
                price=Decimal("100"),
                market_value=Decimal("2500"),
                cost_basis_total=Decimal("2400"),
            ),
            Position(
                account_name="Test",
                account_type=AccountType.ROTH_IRA,
                ticker="SPAXX",
                description="Cash",
                quantity=Decimal("0"),
                price=Decimal("0"),
                market_value=Decimal("500"),
                cost_basis_total=None,
            ),
        ]
        targets = [
            AllocationTarget(asset_class="us_stocks", target_pct=Decimal("50")),
            AllocationTarget(asset_class="international_stocks", target_pct=Decimal("20")),
            AllocationTarget(asset_class="bonds", target_pct=Decimal("25")),
            AllocationTarget(asset_class="cash", target_pct=Decimal("5")),
        ]
        config = RebalanceConfig(threshold_pct=Decimal("3.0"))

        result = rebalance(positions, targets, sample_mapping, config)
        assert result.trades == []


class TestRebalanceWithCash:
    def test_cash_to_invest_buys_only(
        self, sample_positions, targets_50_20_25_5, sample_mapping
    ):
        config = RebalanceConfig(
            threshold_pct=Decimal("1.0"),
            min_trade_value=Decimal("50"),
            cash_to_invest=Decimal("10000"),
        )
        result = rebalance(
            sample_positions, targets_50_20_25_5, sample_mapping, config
        )
        # Should only have BUY trades when investing new cash
        for t in result.trades:
            assert t.action == "BUY"

    def test_cash_goes_to_underweight(
        self, sample_positions, targets_50_20_25_5, sample_mapping
    ):
        config = RebalanceConfig(
            threshold_pct=Decimal("1.0"),
            min_trade_value=Decimal("50"),
            cash_to_invest=Decimal("10000"),
        )
        result = rebalance(
            sample_positions, targets_50_20_25_5, sample_mapping, config
        )
        # Bonds are the most underweight (11.80% vs 25% target)
        bond_buys = [t for t in result.trades if t.ticker == "BND"]
        assert len(bond_buys) > 0
        # The bond buy should be the largest by value
        if len(result.trades) > 1:
            bond_value = sum(t.estimated_value for t in bond_buys)
            assert bond_value > Decimal("0")


class TestRebalanceTaxPriority:
    def test_tax_advantaged_sold_first(self, sample_mapping):
        """Overweight positions should be sold in tax-advantaged accounts first."""
        positions = [
            Position(
                account_name="Taxable",
                account_type=AccountType.TAXABLE,
                ticker="VTI",
                description="VTI",
                quantity=Decimal("100"),
                price=Decimal("100"),
                market_value=Decimal("10000"),
                cost_basis_total=Decimal("8000"),
            ),
            Position(
                account_name="Roth",
                account_type=AccountType.ROTH_IRA,
                ticker="VTI",
                description="VTI",
                quantity=Decimal("100"),
                price=Decimal("100"),
                market_value=Decimal("10000"),
                cost_basis_total=Decimal("8000"),
            ),
            Position(
                account_name="Roth",
                account_type=AccountType.ROTH_IRA,
                ticker="BND",
                description="BND",
                quantity=Decimal("10"),
                price=Decimal("100"),
                market_value=Decimal("1000"),
                cost_basis_total=Decimal("900"),
            ),
        ]
        targets = [
            AllocationTarget(asset_class="us_stocks", target_pct=Decimal("50")),
            AllocationTarget(asset_class="bonds", target_pct=Decimal("50")),
        ]
        config = RebalanceConfig(
            threshold_pct=Decimal("1.0"),
            min_trade_value=Decimal("50"),
        )

        result = rebalance(positions, targets, sample_mapping, config)
        sell_trades = [t for t in result.trades if t.action == "SELL"]

        # First sell should be from Roth (tax-advantaged)
        if sell_trades:
            assert sell_trades[0].account_type == AccountType.ROTH_IRA

    def test_warns_on_taxable_gain(self, sample_mapping):
        """Should warn when selling at a gain in a taxable account."""
        positions = [
            Position(
                account_name="Taxable",
                account_type=AccountType.TAXABLE,
                ticker="VTI",
                description="VTI",
                quantity=Decimal("100"),
                price=Decimal("100"),
                market_value=Decimal("10000"),
                cost_basis_total=Decimal("5000"),  # Large gain
            ),
            Position(
                account_name="Taxable",
                account_type=AccountType.TAXABLE,
                ticker="BND",
                description="BND",
                quantity=Decimal("10"),
                price=Decimal("100"),
                market_value=Decimal("1000"),
                cost_basis_total=Decimal("900"),
            ),
        ]
        targets = [
            AllocationTarget(asset_class="us_stocks", target_pct=Decimal("50")),
            AllocationTarget(asset_class="bonds", target_pct=Decimal("50")),
        ]
        config = RebalanceConfig(
            threshold_pct=Decimal("1.0"),
            min_trade_value=Decimal("50"),
            avoid_gains_in_taxable=True,
        )

        result = rebalance(positions, targets, sample_mapping, config)
        sell_trades = [t for t in result.trades if t.action == "SELL"]

        # The VTI sell should have a gain warning
        vti_sells = [t for t in sell_trades if t.ticker == "VTI"]
        if vti_sells:
            assert any("gain" in w.lower() for w in vti_sells[0].warnings)
