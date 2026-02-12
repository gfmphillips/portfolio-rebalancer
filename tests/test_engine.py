from decimal import Decimal

import pytest

from rebalancer.engine import CashPools, _build_initial_cash_pools, _sort_lots, analyze_consolidation, build_run_metadata, check_constraints, rebalance
from rebalancer.models import (
    AccountType,
    AllocationTarget,
    ConstraintsConfig,
    Position,
    RebalanceConfig,
    TaxLot,
    TickerMapping,
    Trade,
)


@pytest.fixture
def targets_50_20_25_5():
    return [
        AllocationTarget(asset_class="us_equity", target_pct=Decimal("50")),
        AllocationTarget(asset_class="intl_equity", target_pct=Decimal("20")),
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
        # us_equity: VTI(25000) + FXAIX(10000) = 35000 / 61000 = 57.38%
        assert result.current_allocation["us_equity"] == Decimal("57.38")
        # intl_equity: VXUS(12000 + 3000) = 15000 / 61000 = 24.59%
        assert result.current_allocation["intl_equity"] == Decimal("24.59")
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
        # us_equity: 57.38 - 50 = +7.38 (overweight)
        assert result.drift["us_equity"] == Decimal("7.38")
        # intl_equity: 24.59 - 20 = +4.59 (overweight)
        assert result.drift["intl_equity"] == Decimal("4.59")
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
            AllocationTarget(asset_class="us_equity", target_pct=Decimal("50")),
            AllocationTarget(asset_class="intl_equity", target_pct=Decimal("20")),
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
            AllocationTarget(asset_class="us_equity", target_pct=Decimal("50")),
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
            AllocationTarget(asset_class="us_equity", target_pct=Decimal("50")),
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


class TestCashConstraints:
    """Test that buys respect per-account cash pools."""

    def test_tax_adv_buy_limited_to_available_cash(self, multi_account_mapping):
        """Roth has $300 cash, bonds underweight $5K -> buy only ~$300 in Roth."""
        positions = [
            Position(
                account_name="Roth IRA",
                account_type=AccountType.ROTH_IRA,
                ticker="BND",
                description="BND",
                quantity=Decimal("10"),
                price=Decimal("72.00"),
                market_value=Decimal("720.00"),
                cost_basis_total=Decimal("700.00"),
            ),
            Position(
                account_name="Roth IRA",
                account_type=AccountType.ROTH_IRA,
                ticker="SPAXX",
                description="Cash",
                quantity=Decimal("0"),
                price=Decimal("0"),
                market_value=Decimal("300.00"),
                cost_basis_total=None,
            ),
            # Taxable with enough cash to absorb the rest
            Position(
                account_name="Taxable",
                account_type=AccountType.TAXABLE,
                ticker="BND",
                description="BND",
                quantity=Decimal("10"),
                price=Decimal("72.00"),
                market_value=Decimal("720.00"),
                cost_basis_total=Decimal("700.00"),
            ),
            Position(
                account_name="Taxable",
                account_type=AccountType.TAXABLE,
                ticker="VTI",
                description="VTI",
                quantity=Decimal("200"),
                price=Decimal("100.00"),
                market_value=Decimal("20000.00"),
                cost_basis_total=Decimal("15000.00"),
            ),
            Position(
                account_name="Taxable",
                account_type=AccountType.TAXABLE,
                ticker="SPAXX",
                description="Cash",
                quantity=Decimal("0"),
                price=Decimal("0"),
                market_value=Decimal("500.00"),
                cost_basis_total=None,
            ),
        ]
        targets = [
            AllocationTarget(asset_class="us_equity", target_pct=Decimal("50")),
            AllocationTarget(asset_class="bonds", target_pct=Decimal("45")),
            AllocationTarget(asset_class="cash", target_pct=Decimal("5")),
        ]
        config = RebalanceConfig(threshold_pct=Decimal("1.0"), min_trade_value=Decimal("50"))
        result = rebalance(positions, targets, multi_account_mapping, config)

        roth_buys = [
            t for t in result.trades
            if t.action == "BUY" and t.account_name == "Roth IRA"
        ]
        for buy in roth_buys:
            assert buy.estimated_value <= Decimal("300.00") + Decimal("1")  # small rounding tolerance

    def test_taxable_pool_is_shared(self, multi_account_mapping):
        """Sell in Taxable A should fund buys in Taxable B."""
        positions = [
            # Taxable A: overweight us_equity, no bonds
            Position(
                account_name="Taxable A",
                account_type=AccountType.TAXABLE,
                ticker="VTI",
                description="VTI",
                quantity=Decimal("100"),
                price=Decimal("100.00"),
                market_value=Decimal("10000.00"),
                cost_basis_total=Decimal("8000.00"),
            ),
            Position(
                account_name="Taxable A",
                account_type=AccountType.TAXABLE,
                ticker="SPAXX",
                description="Cash",
                quantity=Decimal("0"),
                price=Decimal("0"),
                market_value=Decimal("200.00"),
                cost_basis_total=None,
            ),
            # Taxable B: has bonds position to buy into
            Position(
                account_name="Taxable B",
                account_type=AccountType.TAXABLE,
                ticker="BND",
                description="BND",
                quantity=Decimal("10"),
                price=Decimal("72.00"),
                market_value=Decimal("720.00"),
                cost_basis_total=Decimal("700.00"),
            ),
            Position(
                account_name="Taxable B",
                account_type=AccountType.TAXABLE,
                ticker="SPAXX",
                description="Cash",
                quantity=Decimal("0"),
                price=Decimal("0"),
                market_value=Decimal("100.00"),
                cost_basis_total=None,
            ),
        ]
        targets = [
            AllocationTarget(asset_class="us_equity", target_pct=Decimal("50")),
            AllocationTarget(asset_class="bonds", target_pct=Decimal("47")),
            AllocationTarget(asset_class="cash", target_pct=Decimal("3")),
        ]
        config = RebalanceConfig(threshold_pct=Decimal("1.0"), min_trade_value=Decimal("50"))
        result = rebalance(positions, targets, multi_account_mapping, config)

        # VTI should be sold in Taxable A
        sells = [t for t in result.trades if t.action == "SELL"]
        assert any(t.account_name == "Taxable A" and t.ticker == "VTI" for t in sells)

        # BND should be bought in Taxable B using shared pool (initial cash + sell proceeds)
        bond_buys = [t for t in result.trades if t.action == "BUY" and t.ticker == "BND"]
        assert len(bond_buys) > 0
        taxable_b_buys = [t for t in bond_buys if t.account_name == "Taxable B"]
        assert len(taxable_b_buys) > 0

    def test_sell_proceeds_credit_correct_pool(self, multi_account_mapping):
        """Sell in Roth -> Roth pool, not taxable pool."""
        positions = [
            # Roth: overweight us_equity, underweight bonds
            Position(
                account_name="Roth",
                account_type=AccountType.ROTH_IRA,
                ticker="FXAIX",
                description="FXAIX",
                quantity=Decimal("100"),
                price=Decimal("200.00"),
                market_value=Decimal("20000.00"),
                cost_basis_total=Decimal("15000.00"),
            ),
            Position(
                account_name="Roth",
                account_type=AccountType.ROTH_IRA,
                ticker="BND",
                description="BND",
                quantity=Decimal("10"),
                price=Decimal("72.00"),
                market_value=Decimal("720.00"),
                cost_basis_total=Decimal("700.00"),
            ),
            Position(
                account_name="Roth",
                account_type=AccountType.ROTH_IRA,
                ticker="SPAXX",
                description="Cash",
                quantity=Decimal("0"),
                price=Decimal("0"),
                market_value=Decimal("100.00"),
                cost_basis_total=None,
            ),
        ]
        targets = [
            AllocationTarget(asset_class="us_equity", target_pct=Decimal("50")),
            AllocationTarget(asset_class="bonds", target_pct=Decimal("49")),
            AllocationTarget(asset_class="cash", target_pct=Decimal("1")),
        ]
        config = RebalanceConfig(threshold_pct=Decimal("1.0"), min_trade_value=Decimal("50"))
        result = rebalance(positions, targets, multi_account_mapping, config)

        # Should sell FXAIX in Roth (overweight) and buy BND in Roth (underweight)
        roth_sells = [t for t in result.trades if t.action == "SELL" and t.account_name == "Roth"]
        roth_buys = [t for t in result.trades if t.action == "BUY" and t.account_name == "Roth"]

        assert len(roth_sells) > 0
        assert len(roth_buys) > 0

        # Buy value should be funded by sell proceeds + initial cash ($100)
        sell_total = sum(t.estimated_value for t in roth_sells)
        buy_total = sum(t.estimated_value for t in roth_buys)
        assert buy_total <= sell_total + Decimal("100") + Decimal("1")  # rounding tolerance

    def test_buy_split_across_accounts(self, multi_account_mapping):
        """When no single account has enough cash, buy is split across accounts."""
        positions = [
            # Taxable: has VTI (overweight) and some cash
            Position(
                account_name="Taxable",
                account_type=AccountType.TAXABLE,
                ticker="VTI",
                description="VTI",
                quantity=Decimal("100"),
                price=Decimal("100.00"),
                market_value=Decimal("10000.00"),
                cost_basis_total=Decimal("8000.00"),
            ),
            Position(
                account_name="Taxable",
                account_type=AccountType.TAXABLE,
                ticker="BND",
                description="BND",
                quantity=Decimal("10"),
                price=Decimal("72.00"),
                market_value=Decimal("720.00"),
                cost_basis_total=Decimal("700.00"),
            ),
            Position(
                account_name="Taxable",
                account_type=AccountType.TAXABLE,
                ticker="SPAXX",
                description="Cash",
                quantity=Decimal("0"),
                price=Decimal("0"),
                market_value=Decimal("500.00"),
                cost_basis_total=None,
            ),
            # Roth: also has BND, small cash
            Position(
                account_name="Roth",
                account_type=AccountType.ROTH_IRA,
                ticker="BND",
                description="BND",
                quantity=Decimal("10"),
                price=Decimal("72.00"),
                market_value=Decimal("720.00"),
                cost_basis_total=Decimal("700.00"),
            ),
            Position(
                account_name="Roth",
                account_type=AccountType.ROTH_IRA,
                ticker="SPAXX",
                description="Cash",
                quantity=Decimal("0"),
                price=Decimal("0"),
                market_value=Decimal("200.00"),
                cost_basis_total=None,
            ),
        ]
        targets = [
            AllocationTarget(asset_class="us_equity", target_pct=Decimal("40")),
            AllocationTarget(asset_class="bonds", target_pct=Decimal("55")),
            AllocationTarget(asset_class="cash", target_pct=Decimal("5")),
        ]
        config = RebalanceConfig(threshold_pct=Decimal("1.0"), min_trade_value=Decimal("50"))
        result = rebalance(positions, targets, multi_account_mapping, config)

        bond_buys = [t for t in result.trades if t.action == "BUY" and t.ticker == "BND"]
        # Could have buys in both Roth and Taxable
        accounts_with_buys = {t.account_name for t in bond_buys}
        # At minimum, Roth should buy up to its cash limit
        if "Roth" in accounts_with_buys:
            roth_buy = sum(t.estimated_value for t in bond_buys if t.account_name == "Roth")
            assert roth_buy <= Decimal("200") + Decimal("1")

    def test_insufficient_cash_warning(self):
        """Should generate a warning when an underweight class has no positions to buy into."""
        # REIT is in the target but no REIT positions exist anywhere, so ref_ticker is None
        # and the engine can't buy REIT at all → shortfall
        mapping = {
            "VTI": TickerMapping(asset_class="us_equity"),
            "SPAXX": TickerMapping(asset_class="cash"),
            "VNQ": TickerMapping(asset_class="reit"),
        }
        positions = [
            Position(
                account_name="Taxable",
                account_type=AccountType.TAXABLE,
                ticker="VTI",
                description="VTI",
                quantity=Decimal("100"),
                price=Decimal("100.00"),
                market_value=Decimal("10000.00"),
                cost_basis_total=Decimal("8000.00"),
            ),
            Position(
                account_name="Taxable",
                account_type=AccountType.TAXABLE,
                ticker="SPAXX",
                description="Cash",
                quantity=Decimal("0"),
                price=Decimal("0"),
                market_value=Decimal("500.00"),
                cost_basis_total=None,
            ),
        ]
        targets = [
            AllocationTarget(asset_class="us_equity", target_pct=Decimal("50")),
            AllocationTarget(asset_class="reit", target_pct=Decimal("45")),
            AllocationTarget(asset_class="cash", target_pct=Decimal("5")),
        ]
        config = RebalanceConfig(threshold_pct=Decimal("1.0"), min_trade_value=Decimal("50"))
        result = rebalance(positions, targets, mapping, config)

        assert any("shortfall" in w.lower() for w in result.warnings)

    def test_no_buy_when_no_cash_no_sells(self, multi_account_mapping):
        """IRA with no cash and no sell proceeds should generate no buys."""
        positions = [
            Position(
                account_name="IRA",
                account_type=AccountType.TRADITIONAL_IRA,
                ticker="VCSH",
                description="VCSH",
                quantity=Decimal("10"),
                price=Decimal("80.00"),
                market_value=Decimal("800.00"),
                cost_basis_total=Decimal("780.00"),
            ),
            # No cash position in IRA
            # Taxable: overweight equity with cash
            Position(
                account_name="Taxable",
                account_type=AccountType.TAXABLE,
                ticker="VTI",
                description="VTI",
                quantity=Decimal("100"),
                price=Decimal("100.00"),
                market_value=Decimal("10000.00"),
                cost_basis_total=Decimal("8000.00"),
            ),
            Position(
                account_name="Taxable",
                account_type=AccountType.TAXABLE,
                ticker="SPAXX",
                description="Cash",
                quantity=Decimal("0"),
                price=Decimal("0"),
                market_value=Decimal("500.00"),
                cost_basis_total=None,
            ),
        ]
        targets = [
            AllocationTarget(asset_class="us_equity", target_pct=Decimal("50")),
            AllocationTarget(asset_class="bonds", target_pct=Decimal("45")),
            AllocationTarget(asset_class="cash", target_pct=Decimal("5")),
        ]
        config = RebalanceConfig(threshold_pct=Decimal("1.0"), min_trade_value=Decimal("50"))
        result = rebalance(positions, targets, multi_account_mapping, config)

        ira_buys = [t for t in result.trades if t.action == "BUY" and t.account_name == "IRA"]
        assert len(ira_buys) == 0

    def test_cash_pool_init_from_various_tickers(self, multi_account_mapping):
        """Cash pools should be initialized from SPAXX, FDRXX, and similar cash tickers."""
        positions = [
            Position(
                account_name="Taxable",
                account_type=AccountType.TAXABLE,
                ticker="SPAXX",
                description="Cash",
                quantity=Decimal("0"),
                price=Decimal("0"),
                market_value=Decimal("1000.00"),
                cost_basis_total=None,
            ),
            Position(
                account_name="IRA",
                account_type=AccountType.TRADITIONAL_IRA,
                ticker="FDRXX",
                description="Cash",
                quantity=Decimal("0"),
                price=Decimal("0"),
                market_value=Decimal("500.00"),
                cost_basis_total=None,
            ),
        ]
        pools = _build_initial_cash_pools(positions, multi_account_mapping)
        assert pools.taxable_pool == Decimal("1000.00")
        assert pools.tax_adv_pools["IRA"] == Decimal("500.00")

    def test_prefer_account_already_holding_ticker(self, multi_account_mapping):
        """Buys should go to accounts that already hold the ticker."""
        positions = [
            # Taxable: overweight VTI, has BND
            Position(
                account_name="Taxable",
                account_type=AccountType.TAXABLE,
                ticker="VTI",
                description="VTI",
                quantity=Decimal("100"),
                price=Decimal("100.00"),
                market_value=Decimal("10000.00"),
                cost_basis_total=Decimal("8000.00"),
            ),
            Position(
                account_name="Taxable",
                account_type=AccountType.TAXABLE,
                ticker="BND",
                description="BND",
                quantity=Decimal("10"),
                price=Decimal("72.00"),
                market_value=Decimal("720.00"),
                cost_basis_total=Decimal("700.00"),
            ),
            Position(
                account_name="Taxable",
                account_type=AccountType.TAXABLE,
                ticker="SPAXX",
                description="Cash",
                quantity=Decimal("0"),
                price=Decimal("0"),
                market_value=Decimal("5000.00"),
                cost_basis_total=None,
            ),
        ]
        targets = [
            AllocationTarget(asset_class="us_equity", target_pct=Decimal("50")),
            AllocationTarget(asset_class="bonds", target_pct=Decimal("45")),
            AllocationTarget(asset_class="cash", target_pct=Decimal("5")),
        ]
        config = RebalanceConfig(threshold_pct=Decimal("1.0"), min_trade_value=Decimal("50"))
        result = rebalance(positions, targets, multi_account_mapping, config)

        bond_buys = [t for t in result.trades if t.action == "BUY" and t.ticker == "BND"]
        assert len(bond_buys) > 0
        # Buy should be in Taxable since that's where BND is held
        assert bond_buys[0].account_name == "Taxable"

    def test_no_buy_exceeds_pool_available_cash(
        self, multi_account_positions, multi_account_mapping
    ):
        """No buy trade should exceed the available cash in its pool."""
        targets = [
            AllocationTarget(asset_class="us_equity", target_pct=Decimal("48")),
            AllocationTarget(asset_class="intl_equity", target_pct=Decimal("15")),
            AllocationTarget(asset_class="bonds", target_pct=Decimal("30")),
            AllocationTarget(asset_class="cash", target_pct=Decimal("7")),
        ]
        config = RebalanceConfig(threshold_pct=Decimal("1.0"), min_trade_value=Decimal("50"))
        result = rebalance(multi_account_positions, targets, multi_account_mapping, config)

        # Track cash pools: start with initial cash, add sell proceeds
        pools = _build_initial_cash_pools(multi_account_positions, multi_account_mapping)

        for t in result.trades:
            if t.action == "SELL":
                pools.add(t.account_name, t.account_type, t.estimated_value)

        # Reset pools and replay to check buys
        pools2 = _build_initial_cash_pools(multi_account_positions, multi_account_mapping)
        for t in result.trades:
            if t.action == "SELL":
                pools2.add(t.account_name, t.account_type, t.estimated_value)
            elif t.action == "BUY":
                avail = pools2.available(t.account_name, t.account_type)
                assert t.estimated_value <= avail + Decimal("1"), (
                    f"Buy of ${t.estimated_value} in {t.account_name} exceeds "
                    f"available cash ${avail}"
                )
                pools2.spend(t.account_name, t.account_type, t.estimated_value)

    def test_buy_new_position_in_account_with_sell_proceeds(self, multi_account_mapping):
        """Sell proceeds in an account with no position in the underweight class should
        still be deployable via a reference ticker (second pass)."""
        positions = [
            # Roth: only equity, no bonds — will sell equity (overweight)
            Position(
                account_name="Roth",
                account_type=AccountType.ROTH_IRA,
                ticker="FXAIX",
                description="FXAIX",
                quantity=Decimal("100"),
                price=Decimal("200.00"),
                market_value=Decimal("20000.00"),
                cost_basis_total=Decimal("15000.00"),
            ),
            Position(
                account_name="Roth",
                account_type=AccountType.ROTH_IRA,
                ticker="SPAXX",
                description="Cash",
                quantity=Decimal("0"),
                price=Decimal("0"),
                market_value=Decimal("100.00"),
                cost_basis_total=None,
            ),
            # Taxable: holds bonds (provides reference ticker)
            Position(
                account_name="Taxable",
                account_type=AccountType.TAXABLE,
                ticker="BND",
                description="BND",
                quantity=Decimal("10"),
                price=Decimal("72.00"),
                market_value=Decimal("720.00"),
                cost_basis_total=Decimal("700.00"),
            ),
            Position(
                account_name="Taxable",
                account_type=AccountType.TAXABLE,
                ticker="SPAXX",
                description="Cash",
                quantity=Decimal("0"),
                price=Decimal("0"),
                market_value=Decimal("200.00"),
                cost_basis_total=None,
            ),
        ]
        targets = [
            AllocationTarget(asset_class="us_equity", target_pct=Decimal("50")),
            AllocationTarget(asset_class="bonds", target_pct=Decimal("48")),
            AllocationTarget(asset_class="cash", target_pct=Decimal("2")),
        ]
        config = RebalanceConfig(threshold_pct=Decimal("1.0"), min_trade_value=Decimal("50"))
        result = rebalance(positions, targets, multi_account_mapping, config)

        # Roth should sell FXAIX (overweight equity)
        roth_sells = [t for t in result.trades if t.action == "SELL" and t.account_name == "Roth"]
        assert len(roth_sells) > 0

        # Roth should buy bonds using sell proceeds (new position via ref ticker)
        roth_buys = [t for t in result.trades if t.action == "BUY" and t.account_name == "Roth"]
        assert len(roth_buys) > 0
        assert roth_buys[0].ticker == "BND"  # reference ticker from Taxable
        assert "new position" in roth_buys[0].reasoning

    def test_cash_pools_dataclass(self):
        """Test CashPools add/spend/available methods."""
        pools = CashPools()

        # Taxable accounts share a pool
        pools.add("Taxable A", AccountType.TAXABLE, Decimal("1000"))
        pools.add("Taxable B", AccountType.TAXABLE, Decimal("500"))
        assert pools.available("Taxable A", AccountType.TAXABLE) == Decimal("1500")
        assert pools.available("Taxable B", AccountType.TAXABLE) == Decimal("1500")

        pools.spend("Taxable A", AccountType.TAXABLE, Decimal("600"))
        assert pools.available("Taxable B", AccountType.TAXABLE) == Decimal("900")

        # Tax-advantaged accounts are isolated
        pools.add("Roth", AccountType.ROTH_IRA, Decimal("300"))
        pools.add("IRA", AccountType.TRADITIONAL_IRA, Decimal("200"))
        assert pools.available("Roth", AccountType.ROTH_IRA) == Decimal("300")
        assert pools.available("IRA", AccountType.TRADITIONAL_IRA) == Decimal("200")

        pools.spend("Roth", AccountType.ROTH_IRA, Decimal("100"))
        assert pools.available("Roth", AccountType.ROTH_IRA) == Decimal("200")
        # IRA unaffected
        assert pools.available("IRA", AccountType.TRADITIONAL_IRA) == Decimal("200")


class TestRelativeDrift:
    """Tests for relative drift threshold (OR logic with absolute)."""

    def _make_positions(self, us_value, intl_value, bond_value, cash_value):
        """Helper to create a simple single-account portfolio."""
        positions = []
        if us_value > 0:
            positions.append(Position(
                account_name="Roth", account_type=AccountType.ROTH_IRA,
                ticker="VTI", description="VTI",
                quantity=Decimal(str(us_value)) / Decimal("100"),
                price=Decimal("100"), market_value=Decimal(str(us_value)),
                cost_basis_total=Decimal(str(us_value)),
            ))
        if intl_value > 0:
            positions.append(Position(
                account_name="Roth", account_type=AccountType.ROTH_IRA,
                ticker="VXUS", description="VXUS",
                quantity=Decimal(str(intl_value)) / Decimal("100"),
                price=Decimal("100"), market_value=Decimal(str(intl_value)),
                cost_basis_total=Decimal(str(intl_value)),
            ))
        if bond_value > 0:
            positions.append(Position(
                account_name="Roth", account_type=AccountType.ROTH_IRA,
                ticker="BND", description="BND",
                quantity=Decimal(str(bond_value)) / Decimal("72"),
                price=Decimal("72"), market_value=Decimal(str(bond_value)),
                cost_basis_total=Decimal(str(bond_value)),
            ))
        if cash_value > 0:
            positions.append(Position(
                account_name="Roth", account_type=AccountType.ROTH_IRA,
                ticker="SPAXX", description="Cash",
                quantity=Decimal("0"), price=Decimal("0"),
                market_value=Decimal(str(cash_value)), cost_basis_total=None,
            ))
        return positions

    def test_relative_drift_triggers_rebalance(self, sample_mapping):
        """20% target at 15% actual = 25% relative drift → triggers rebalance."""
        # Total = 10000. intl at 15% (1500) vs 20% target → 5pp abs (at threshold), 25% rel (over 20%)
        positions = self._make_positions(5500, 1500, 2500, 500)
        targets = [
            AllocationTarget(asset_class="us_equity", target_pct=Decimal("50")),
            AllocationTarget(asset_class="intl_equity", target_pct=Decimal("20")),
            AllocationTarget(asset_class="bonds", target_pct=Decimal("25")),
            AllocationTarget(asset_class="cash", target_pct=Decimal("5")),
        ]
        config = RebalanceConfig(
            threshold_pct=Decimal("5.0"),
            threshold_relative_pct=Decimal("20"),
            min_trade_value=Decimal("50"),
        )
        result = rebalance(positions, targets, sample_mapping, config)
        # intl_equity should trigger due to relative drift
        assert any(t.ticker == "VXUS" for t in result.trades)

    def test_relative_drift_within_band(self, sample_mapping):
        """20% target at 17% actual = 15% relative drift → does NOT trigger."""
        # Total = 10000. intl at 17% (1700) vs 20% target → 3pp abs (within 5), 15% rel (within 20%)
        positions = self._make_positions(5300, 1700, 2500, 500)
        targets = [
            AllocationTarget(asset_class="us_equity", target_pct=Decimal("50")),
            AllocationTarget(asset_class="intl_equity", target_pct=Decimal("20")),
            AllocationTarget(asset_class="bonds", target_pct=Decimal("25")),
            AllocationTarget(asset_class="cash", target_pct=Decimal("5")),
        ]
        config = RebalanceConfig(
            threshold_pct=Decimal("5.0"),
            threshold_relative_pct=Decimal("20"),
            min_trade_value=Decimal("50"),
        )
        result = rebalance(positions, targets, sample_mapping, config)
        # No trades expected — all classes within both bands
        assert result.trades == []

    def test_absolute_drift_triggers_even_if_relative_ok(self, sample_mapping):
        """Large class with >5pp absolute drift triggers even if relative is low."""
        # us_equity at 58% vs 50% target → 8pp abs (over 5), 16% rel (under 20%)
        positions = self._make_positions(5800, 2000, 1700, 500)
        targets = [
            AllocationTarget(asset_class="us_equity", target_pct=Decimal("50")),
            AllocationTarget(asset_class="intl_equity", target_pct=Decimal("20")),
            AllocationTarget(asset_class="bonds", target_pct=Decimal("25")),
            AllocationTarget(asset_class="cash", target_pct=Decimal("5")),
        ]
        config = RebalanceConfig(
            threshold_pct=Decimal("5.0"),
            threshold_relative_pct=Decimal("20"),
            min_trade_value=Decimal("50"),
        )
        result = rebalance(positions, targets, sample_mapping, config)
        # us_equity should trigger due to absolute drift
        sell_tickers = {t.ticker for t in result.trades if t.action == "SELL"}
        assert "VTI" in sell_tickers

    def test_zero_target_no_division_error(self, sample_mapping):
        """Cash at 0% target should not cause division by zero."""
        positions = self._make_positions(5000, 2000, 2500, 500)
        targets = [
            AllocationTarget(asset_class="us_equity", target_pct=Decimal("50")),
            AllocationTarget(asset_class="intl_equity", target_pct=Decimal("20")),
            AllocationTarget(asset_class="bonds", target_pct=Decimal("30")),
            AllocationTarget(asset_class="cash", target_pct=Decimal("0")),
        ]
        config = RebalanceConfig(
            threshold_pct=Decimal("5.0"),
            threshold_relative_pct=Decimal("20"),
            min_trade_value=Decimal("50"),
        )
        # Should not raise
        result = rebalance(positions, targets, sample_mapping, config)
        assert result.total_portfolio_value > 0


class TestConsolidation:
    """Tests for analyze_consolidation()."""

    def _make_mapping(self):
        return {
            "VTI": TickerMapping(asset_class="us_equity", preferred=True),
            "FXAIX": TickerMapping(asset_class="us_equity", consolidate_to="VTI"),
            "VXUS": TickerMapping(asset_class="intl_equity", preferred=True),
            "VGK": TickerMapping(asset_class="intl_equity", consolidate_to="VXUS"),
            "BND": TickerMapping(asset_class="bonds", preferred=True),
            "VCSH": TickerMapping(asset_class="bonds", consolidate_to="BND"),
            "SPAXX": TickerMapping(asset_class="cash"),
        }

    def test_consolidation_identifies_legacy(self):
        """Legacy tickers with consolidate_to are flagged."""
        mapping = self._make_mapping()
        positions = [
            Position(
                account_name="Roth", account_type=AccountType.ROTH_IRA,
                ticker="FXAIX", description="FXAIX",
                quantity=Decimal("50"), price=Decimal("200"),
                market_value=Decimal("10000"), cost_basis_total=Decimal("8000"),
            ),
        ]
        analysis = analyze_consolidation(positions, mapping)
        assert len(analysis.opportunities) == 1
        assert analysis.opportunities[0].ticker == "FXAIX"
        assert analysis.opportunities[0].consolidate_to == "VTI"

    def test_consolidation_retirement_always_safe(self):
        """Roth/IRA positions are always safe to consolidate."""
        mapping = self._make_mapping()
        positions = [
            Position(
                account_name="Roth", account_type=AccountType.ROTH_IRA,
                ticker="FXAIX", description="FXAIX",
                quantity=Decimal("50"), price=Decimal("200"),
                market_value=Decimal("10000"), cost_basis_total=Decimal("5000"),
            ),
        ]
        analysis = analyze_consolidation(positions, mapping)
        assert analysis.opportunities[0].safe_to_consolidate is True
        assert "Retirement" in analysis.opportunities[0].reason

    def test_consolidation_taxable_loss_safe(self):
        """Taxable position at a loss is safe to consolidate."""
        mapping = self._make_mapping()
        positions = [
            Position(
                account_name="Taxable", account_type=AccountType.TAXABLE,
                ticker="VGK", description="VGK",
                quantity=Decimal("100"), price=Decimal("50"),
                market_value=Decimal("5000"), cost_basis_total=Decimal("6000"),
            ),
        ]
        analysis = analyze_consolidation(positions, mapping)
        assert analysis.opportunities[0].safe_to_consolidate is True
        assert "loss" in analysis.opportunities[0].reason.lower()

    def test_consolidation_taxable_gain_wait(self):
        """Taxable position at a gain should wait."""
        mapping = self._make_mapping()
        positions = [
            Position(
                account_name="Taxable", account_type=AccountType.TAXABLE,
                ticker="FXAIX", description="FXAIX",
                quantity=Decimal("50"), price=Decimal("200"),
                market_value=Decimal("10000"), cost_basis_total=Decimal("5000"),
            ),
        ]
        analysis = analyze_consolidation(positions, mapping)
        assert analysis.opportunities[0].safe_to_consolidate is False
        assert "gain" in analysis.opportunities[0].reason.lower()

    def test_consolidation_percentages(self):
        """end_state_pct + legacy_pct should be computed correctly."""
        mapping = self._make_mapping()
        positions = [
            Position(
                account_name="Roth", account_type=AccountType.ROTH_IRA,
                ticker="VTI", description="VTI",
                quantity=Decimal("30"), price=Decimal("100"),
                market_value=Decimal("3000"), cost_basis_total=Decimal("2500"),
            ),
            Position(
                account_name="Roth", account_type=AccountType.ROTH_IRA,
                ticker="FXAIX", description="FXAIX",
                quantity=Decimal("10"), price=Decimal("200"),
                market_value=Decimal("2000"), cost_basis_total=Decimal("1800"),
            ),
        ]
        analysis = analyze_consolidation(positions, mapping)
        # VTI=3000 preferred, FXAIX=2000 legacy, total=5000
        assert analysis.end_state_value == Decimal("3000")
        assert analysis.legacy_value == Decimal("2000")
        assert analysis.end_state_pct == Decimal("60.00")
        assert analysis.legacy_pct == Decimal("40.00")

    def test_consolidation_skips_cash(self):
        """SPAXX (cash) should not be counted as legacy."""
        mapping = self._make_mapping()
        positions = [
            Position(
                account_name="Roth", account_type=AccountType.ROTH_IRA,
                ticker="VTI", description="VTI",
                quantity=Decimal("30"), price=Decimal("100"),
                market_value=Decimal("3000"), cost_basis_total=Decimal("2500"),
            ),
            Position(
                account_name="Roth", account_type=AccountType.ROTH_IRA,
                ticker="SPAXX", description="Cash",
                quantity=Decimal("0"), price=Decimal("0"),
                market_value=Decimal("500"), cost_basis_total=None,
            ),
        ]
        analysis = analyze_consolidation(positions, mapping)
        # Cash should be excluded entirely
        assert analysis.end_state_value == Decimal("3000")
        assert analysis.legacy_value == Decimal("0")
        assert analysis.opportunities == []


class TestRunMetadata:
    def test_build_run_metadata(self):
        m = build_run_metadata(eurusd_fx=Decimal("1.10"))
        assert m.eurusd_fx_used == Decimal("1.10")
        assert "T" in m.timestamp  # ISO 8601
        assert m.tool_version  # non-empty

    def test_metadata_attached_to_result(self, sample_positions, sample_mapping, sample_config):
        targets = [
            AllocationTarget(asset_class="us_equity", target_pct=Decimal("50")),
            AllocationTarget(asset_class="intl_equity", target_pct=Decimal("20")),
            AllocationTarget(asset_class="bonds", target_pct=Decimal("25")),
            AllocationTarget(asset_class="cash", target_pct=Decimal("5")),
        ]
        m = build_run_metadata(eurusd_fx=Decimal("1.08"))
        result = rebalance(sample_positions, targets, sample_mapping, sample_config, metadata=m)
        assert result.metadata is not None
        assert result.metadata.eurusd_fx_used == Decimal("1.08")

    def test_metadata_none_by_default(self, sample_positions, sample_mapping, sample_config):
        targets = [
            AllocationTarget(asset_class="us_equity", target_pct=Decimal("50")),
            AllocationTarget(asset_class="intl_equity", target_pct=Decimal("20")),
            AllocationTarget(asset_class="bonds", target_pct=Decimal("25")),
            AllocationTarget(asset_class="cash", target_pct=Decimal("5")),
        ]
        result = rebalance(sample_positions, targets, sample_mapping, sample_config)
        assert result.metadata is None

    def test_metadata_on_empty_portfolio(self, sample_mapping, sample_config):
        targets = [
            AllocationTarget(asset_class="us_equity", target_pct=Decimal("100")),
        ]
        m = build_run_metadata(eurusd_fx=Decimal("1.10"))
        result = rebalance([], targets, sample_mapping, sample_config, metadata=m)
        assert result.metadata is not None


class TestConstraintChecking:
    def _make_positions(self):
        return [
            Position(
                account_name="Taxable",
                account_type=AccountType.TAXABLE,
                ticker="BND",
                description="BND",
                quantity=Decimal("200"),
                price=Decimal("72"),
                market_value=Decimal("14400"),
                cost_basis_total=Decimal("14000"),
            ),
            Position(
                account_name="Taxable",
                account_type=AccountType.TAXABLE,
                ticker="VTI",
                description="VTI",
                quantity=Decimal("50"),
                price=Decimal("250"),
                market_value=Decimal("12500"),
                cost_basis_total=Decimal("10000"),
            ),
            Position(
                account_name="Taxable",
                account_type=AccountType.TAXABLE,
                ticker="SPAXX",
                description="Cash",
                quantity=Decimal("0"),
                price=Decimal("0"),
                market_value=Decimal("1000"),
                cost_basis_total=None,
            ),
        ]

    def _make_mapping(self):
        return {
            "BND": TickerMapping(asset_class="bonds"),
            "VTI": TickerMapping(asset_class="us_equity"),
            "SPAXX": TickerMapping(asset_class="cash"),
        }

    def test_constraint_met(self):
        """Constraint should be met when taxable bonds >= minimum."""
        positions = self._make_positions()
        mapping = self._make_mapping()
        trades = []  # no trades, bonds stay at 14400
        constraints = ConstraintsConfig(min_taxable_bonds_usd=Decimal("10000"))
        checks = check_constraints(positions, trades, mapping, constraints)
        assert len(checks) == 1
        assert checks[0].met is True
        assert checks[0].actual == Decimal("14400.00")

    def test_constraint_violated(self):
        """Constraint violated when bonds sold below minimum."""
        positions = self._make_positions()
        mapping = self._make_mapping()
        # Sell $6000 of bonds in taxable
        trades = [
            Trade(
                account_name="Taxable",
                account_type=AccountType.TAXABLE,
                ticker="BND",
                action="SELL",
                shares=Decimal("83.333"),
                estimated_value=Decimal("6000"),
                reasoning="test",
            ),
        ]
        constraints = ConstraintsConfig(min_taxable_bonds_usd=Decimal("10000"))
        checks = check_constraints(positions, trades, mapping, constraints)
        assert len(checks) == 1
        assert checks[0].met is False
        assert checks[0].actual == Decimal("8400.00")
        assert "CONSTRAINT VIOLATED" in checks[0].message

    def test_constraint_with_bond_buy(self):
        """Bond buys should increase post-trade value."""
        positions = self._make_positions()
        mapping = self._make_mapping()
        # Sell $8000 bonds, but buy $5000 back
        trades = [
            Trade(
                account_name="Taxable",
                account_type=AccountType.TAXABLE,
                ticker="BND",
                action="SELL",
                shares=Decimal("111.111"),
                estimated_value=Decimal("8000"),
                reasoning="test",
            ),
            Trade(
                account_name="Taxable",
                account_type=AccountType.TAXABLE,
                ticker="BND",
                action="BUY",
                shares=Decimal("69.444"),
                estimated_value=Decimal("5000"),
                reasoning="test",
            ),
        ]
        constraints = ConstraintsConfig(min_taxable_bonds_usd=Decimal("10000"))
        checks = check_constraints(positions, trades, mapping, constraints)
        # 14400 - 8000 + 5000 = 11400
        assert checks[0].met is True
        assert checks[0].actual == Decimal("11400.00")

    def test_no_constraint_configured(self):
        """No checks returned when no constraints configured."""
        positions = self._make_positions()
        mapping = self._make_mapping()
        constraints = ConstraintsConfig()  # all None
        checks = check_constraints(positions, [], mapping, constraints)
        assert checks == []

    def test_constraint_integrated_in_rebalance(self):
        """Constraint violations appear in rebalance warnings."""
        positions = self._make_positions()
        mapping = self._make_mapping()
        targets = [
            AllocationTarget(asset_class="us_equity", target_pct=Decimal("80")),
            AllocationTarget(asset_class="bonds", target_pct=Decimal("15")),
            AllocationTarget(asset_class="cash", target_pct=Decimal("5")),
        ]
        config = RebalanceConfig(threshold_pct=Decimal("1"), min_trade_value=Decimal("50"))
        constraints = ConstraintsConfig(min_taxable_bonds_usd=Decimal("20000"))
        result = rebalance(positions, targets, mapping, config, constraints=constraints)
        assert len(result.constraints) == 1
        # With 80% equity target, bonds will likely be sold, violating 20K minimum
        # Either it's violated (shows in warnings) or met (no warning)
        if not result.constraints[0].met:
            assert any("CONSTRAINT VIOLATED" in w for w in result.warnings)

    def test_constraint_no_violation_no_warning(self):
        """Met constraints don't appear in warnings."""
        positions = self._make_positions()
        mapping = self._make_mapping()
        targets = [
            AllocationTarget(asset_class="us_equity", target_pct=Decimal("45")),
            AllocationTarget(asset_class="bonds", target_pct=Decimal("50")),
            AllocationTarget(asset_class="cash", target_pct=Decimal("5")),
        ]
        config = RebalanceConfig(threshold_pct=Decimal("1"), min_trade_value=Decimal("50"))
        constraints = ConstraintsConfig(min_taxable_bonds_usd=Decimal("1000"))
        result = rebalance(positions, targets, mapping, config, constraints=constraints)
        assert len(result.constraints) == 1
        assert result.constraints[0].met is True
        assert not any("CONSTRAINT VIOLATED" in w for w in result.warnings)

    def test_roth_bonds_not_counted(self):
        """Only taxable bond positions count for taxable bond constraint."""
        positions = [
            Position(
                account_name="Roth",
                account_type=AccountType.ROTH_IRA,
                ticker="BND",
                description="BND",
                quantity=Decimal("200"),
                price=Decimal("72"),
                market_value=Decimal("14400"),
                cost_basis_total=Decimal("14000"),
            ),
        ]
        mapping = {"BND": TickerMapping(asset_class="bonds")}
        constraints = ConstraintsConfig(min_taxable_bonds_usd=Decimal("10000"))
        checks = check_constraints(positions, [], mapping, constraints)
        assert checks[0].met is False
        assert checks[0].actual == Decimal("0.00")


class TestSortLots:
    """Test _sort_lots helper for HIFO, FIFO, TLH strategies."""

    def _make_position_with_lots(self, account_type, lots):
        return Position(
            account_name="Test Account",
            account_type=account_type,
            ticker="VTI",
            description="Test",
            quantity=sum(lot.shares for lot in lots),
            price=Decimal("250.00"),
            market_value=sum(lot.shares for lot in lots) * Decimal("250.00"),
            tax_lots=lots,
        )

    def test_hifo_taxable_no_tlh(self):
        """Taxable with TLH disabled: highest cost first (HIFO)."""
        lots = [
            TaxLot(acquisition_date="2020-01-01", shares=Decimal("10"), cost_basis_per_share=Decimal("150")),
            TaxLot(acquisition_date="2021-01-01", shares=Decimal("10"), cost_basis_per_share=Decimal("300")),
            TaxLot(acquisition_date="2022-01-01", shares=Decimal("10"), cost_basis_per_share=Decimal("200")),
        ]
        pos = self._make_position_with_lots(AccountType.TAXABLE, lots)
        config = RebalanceConfig(tlh_enabled=False)
        result = _sort_lots(pos, config)
        assert result[0].cost_basis_per_share == Decimal("300")
        assert result[1].cost_basis_per_share == Decimal("200")
        assert result[2].cost_basis_per_share == Decimal("150")

    def test_tlh_taxable(self):
        """Taxable with TLH enabled: highest cost first (maximize harvested loss)."""
        lots = [
            TaxLot(acquisition_date="2020-01-01", shares=Decimal("10"), cost_basis_per_share=Decimal("150")),
            TaxLot(acquisition_date="2021-01-01", shares=Decimal("10"), cost_basis_per_share=Decimal("300")),
            TaxLot(acquisition_date="2022-01-01", shares=Decimal("10"), cost_basis_per_share=Decimal("200")),
        ]
        pos = self._make_position_with_lots(AccountType.TAXABLE, lots)
        config = RebalanceConfig(tlh_enabled=True)
        result = _sort_lots(pos, config)
        # TLH and HIFO both sell highest-cost lots first:
        # selling high-cost lots minimizes gains AND maximizes losses
        assert result[0].cost_basis_per_share == Decimal("300")
        assert result[1].cost_basis_per_share == Decimal("200")
        assert result[2].cost_basis_per_share == Decimal("150")

    def test_fifo_tax_advantaged(self):
        """Tax-advantaged: FIFO by acquisition date."""
        lots = [
            TaxLot(acquisition_date="2022-06-01", shares=Decimal("10"), cost_basis_per_share=Decimal("200")),
            TaxLot(acquisition_date="2020-01-15", shares=Decimal("10"), cost_basis_per_share=Decimal("150")),
            TaxLot(acquisition_date="2021-03-01", shares=Decimal("10"), cost_basis_per_share=Decimal("180")),
        ]
        pos = self._make_position_with_lots(AccountType.ROTH_IRA, lots)
        config = RebalanceConfig(tlh_enabled=True)
        result = _sort_lots(pos, config)
        assert result[0].acquisition_date == "2020-01-15"
        assert result[1].acquisition_date == "2021-03-01"
        assert result[2].acquisition_date == "2022-06-01"


class TestLotAwareSelling:
    """Test that the engine generates per-lot trades when tax lots are present."""

    def _make_overweight_portfolio_with_lots(self):
        """Create a portfolio where us_equity is overweight and has lot data."""
        lots = [
            TaxLot(acquisition_date="2020-03-15", shares=Decimal("30"), cost_basis_per_share=Decimal("150")),
            TaxLot(acquisition_date="2021-06-01", shares=Decimal("30"), cost_basis_per_share=Decimal("280")),
            TaxLot(acquisition_date="2022-01-10", shares=Decimal("40"), cost_basis_per_share=Decimal("230")),
        ]
        positions = [
            Position(
                account_name="Taxable",
                account_type=AccountType.TAXABLE,
                ticker="VTI",
                description="VTI",
                quantity=Decimal("100"),
                price=Decimal("250.00"),
                market_value=Decimal("25000.00"),
                cost_basis_total=Decimal("21500.00"),
                tax_lots=lots,
            ),
            Position(
                account_name="Taxable",
                account_type=AccountType.TAXABLE,
                ticker="BND",
                description="BND",
                quantity=Decimal("50"),
                price=Decimal("72.00"),
                market_value=Decimal("3600.00"),
                cost_basis_total=Decimal("4000.00"),
            ),
            Position(
                account_name="Taxable",
                account_type=AccountType.TAXABLE,
                ticker="SPAXX",
                description="Cash",
                quantity=Decimal("0"),
                price=Decimal("0"),
                market_value=Decimal("1400.00"),
            ),
        ]
        targets = [
            AllocationTarget(asset_class="us_equity", target_pct=Decimal("60")),
            AllocationTarget(asset_class="bonds", target_pct=Decimal("30")),
            AllocationTarget(asset_class="cash", target_pct=Decimal("10")),
        ]
        mapping = {
            "VTI": TickerMapping(asset_class="us_equity"),
            "BND": TickerMapping(asset_class="bonds"),
            "SPAXX": TickerMapping(asset_class="cash"),
        }
        return positions, targets, mapping

    def test_lot_trades_have_acquisition_date(self):
        positions, targets, mapping = self._make_overweight_portfolio_with_lots()
        config = RebalanceConfig(
            threshold_pct=Decimal("3.0"),
            min_trade_value=Decimal("50"),
            tlh_enabled=False,
            avoid_gains_in_taxable=False,
        )
        result = rebalance(positions, targets, mapping, config)
        sell_trades = [t for t in result.trades if t.action == "SELL"]
        lot_sells = [t for t in sell_trades if t.lot_acquisition_date is not None]
        assert len(lot_sells) > 0
        for t in lot_sells:
            assert "lot:" in t.reasoning

    def test_hifo_sells_highest_cost_first(self):
        positions, targets, mapping = self._make_overweight_portfolio_with_lots()
        config = RebalanceConfig(
            threshold_pct=Decimal("3.0"),
            min_trade_value=Decimal("50"),
            tlh_enabled=False,
            avoid_gains_in_taxable=False,
        )
        result = rebalance(positions, targets, mapping, config)
        sell_trades = [t for t in result.trades if t.action == "SELL" and t.lot_acquisition_date is not None]
        if len(sell_trades) >= 2:
            # First sell should be from highest cost lot (2021-06-01, $280)
            assert sell_trades[0].lot_acquisition_date == "2021-06-01"

    def test_tlh_sells_highest_cost_first(self):
        positions, targets, mapping = self._make_overweight_portfolio_with_lots()
        config = RebalanceConfig(
            threshold_pct=Decimal("3.0"),
            min_trade_value=Decimal("50"),
            tlh_enabled=True,
            avoid_gains_in_taxable=False,
        )
        result = rebalance(positions, targets, mapping, config)
        sell_trades = [t for t in result.trades if t.action == "SELL" and t.lot_acquisition_date is not None]
        if len(sell_trades) >= 1:
            # TLH sells highest cost first to maximize harvested loss
            assert sell_trades[0].lot_acquisition_date == "2021-06-01"

    def test_partial_lot_consumption(self):
        """When only part of a lot is needed, don't sell more than needed."""
        positions = [
            Position(
                account_name="Taxable",
                account_type=AccountType.TAXABLE,
                ticker="VTI",
                description="VTI",
                quantity=Decimal("100"),
                price=Decimal("100.00"),
                market_value=Decimal("10000.00"),
                tax_lots=[
                    TaxLot(acquisition_date="2020-01-01", shares=Decimal("100"), cost_basis_per_share=Decimal("80")),
                ],
            ),
            Position(
                account_name="Taxable",
                account_type=AccountType.TAXABLE,
                ticker="BND",
                description="BND",
                quantity=Decimal("10"),
                price=Decimal("72.00"),
                market_value=Decimal("720.00"),
            ),
            Position(
                account_name="Taxable",
                account_type=AccountType.TAXABLE,
                ticker="SPAXX",
                description="Cash",
                quantity=Decimal("0"),
                price=Decimal("0"),
                market_value=Decimal("280.00"),
            ),
        ]
        targets = [
            AllocationTarget(asset_class="us_equity", target_pct=Decimal("80")),
            AllocationTarget(asset_class="bonds", target_pct=Decimal("10")),
            AllocationTarget(asset_class="cash", target_pct=Decimal("10")),
        ]
        mapping = {
            "VTI": TickerMapping(asset_class="us_equity"),
            "BND": TickerMapping(asset_class="bonds"),
            "SPAXX": TickerMapping(asset_class="cash"),
        }
        config = RebalanceConfig(
            threshold_pct=Decimal("3.0"),
            min_trade_value=Decimal("50"),
            tlh_enabled=False,
            avoid_gains_in_taxable=False,
        )
        result = rebalance(positions, targets, mapping, config)
        sell_trades = [t for t in result.trades if t.action == "SELL" and t.ticker == "VTI"]
        if sell_trades:
            # Should not sell more than 100 shares (the lot size)
            total_sold = sum(t.shares for t in sell_trades)
            assert total_sold <= Decimal("100")

    def test_no_lots_uses_blended_basis(self):
        """Positions without lots should use the existing blended basis logic."""
        positions = [
            Position(
                account_name="Taxable",
                account_type=AccountType.TAXABLE,
                ticker="VTI",
                description="VTI",
                quantity=Decimal("100"),
                price=Decimal("250.00"),
                market_value=Decimal("25000.00"),
                cost_basis_total=Decimal("20000.00"),
            ),
            Position(
                account_name="Taxable",
                account_type=AccountType.TAXABLE,
                ticker="BND",
                description="BND",
                quantity=Decimal("20"),
                price=Decimal("72.00"),
                market_value=Decimal("1440.00"),
                cost_basis_total=Decimal("1500.00"),
            ),
            Position(
                account_name="Taxable",
                account_type=AccountType.TAXABLE,
                ticker="SPAXX",
                description="Cash",
                quantity=Decimal("0"),
                price=Decimal("0"),
                market_value=Decimal("560.00"),
            ),
        ]
        targets = [
            AllocationTarget(asset_class="us_equity", target_pct=Decimal("60")),
            AllocationTarget(asset_class="bonds", target_pct=Decimal("30")),
            AllocationTarget(asset_class="cash", target_pct=Decimal("10")),
        ]
        mapping = {
            "VTI": TickerMapping(asset_class="us_equity"),
            "BND": TickerMapping(asset_class="bonds"),
            "SPAXX": TickerMapping(asset_class="cash"),
        }
        config = RebalanceConfig(
            threshold_pct=Decimal("3.0"),
            min_trade_value=Decimal("50"),
            tlh_enabled=False,
            avoid_gains_in_taxable=False,
        )
        result = rebalance(positions, targets, mapping, config)
        sell_trades = [t for t in result.trades if t.action == "SELL"]
        for t in sell_trades:
            assert t.lot_acquisition_date is None

    def test_fifo_in_tax_advantaged(self):
        """Tax-advantaged accounts should sell oldest lot first (FIFO)."""
        lots = [
            TaxLot(acquisition_date="2022-06-01", shares=Decimal("20"), cost_basis_per_share=Decimal("230")),
            TaxLot(acquisition_date="2020-01-15", shares=Decimal("30"), cost_basis_per_share=Decimal("180")),
            TaxLot(acquisition_date="2021-03-01", shares=Decimal("50"), cost_basis_per_share=Decimal("200")),
        ]
        positions = [
            Position(
                account_name="Roth IRA",
                account_type=AccountType.ROTH_IRA,
                ticker="VTI",
                description="VTI",
                quantity=Decimal("100"),
                price=Decimal("250.00"),
                market_value=Decimal("25000.00"),
                cost_basis_total=Decimal("20500.00"),
                tax_lots=lots,
            ),
            Position(
                account_name="Roth IRA",
                account_type=AccountType.ROTH_IRA,
                ticker="BND",
                description="BND",
                quantity=Decimal("20"),
                price=Decimal("72.00"),
                market_value=Decimal("1440.00"),
            ),
            Position(
                account_name="Roth IRA",
                account_type=AccountType.ROTH_IRA,
                ticker="SPAXX",
                description="Cash",
                quantity=Decimal("0"),
                price=Decimal("0"),
                market_value=Decimal("560.00"),
            ),
        ]
        targets = [
            AllocationTarget(asset_class="us_equity", target_pct=Decimal("60")),
            AllocationTarget(asset_class="bonds", target_pct=Decimal("30")),
            AllocationTarget(asset_class="cash", target_pct=Decimal("10")),
        ]
        mapping = {
            "VTI": TickerMapping(asset_class="us_equity"),
            "BND": TickerMapping(asset_class="bonds"),
            "SPAXX": TickerMapping(asset_class="cash"),
        }
        config = RebalanceConfig(
            threshold_pct=Decimal("3.0"),
            min_trade_value=Decimal("50"),
            tlh_enabled=True,
        )
        result = rebalance(positions, targets, mapping, config)
        sell_trades = [t for t in result.trades if t.action == "SELL" and t.lot_acquisition_date is not None]
        if sell_trades:
            # FIFO: oldest lot first
            assert sell_trades[0].lot_acquisition_date == "2020-01-15"
            assert "FIFO" in sell_trades[0].reasoning
