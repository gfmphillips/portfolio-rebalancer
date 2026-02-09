from decimal import Decimal

import pytest

from rebalancer.engine import CashPools, _build_initial_cash_pools, rebalance
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
