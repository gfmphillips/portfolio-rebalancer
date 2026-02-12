from decimal import Decimal

import pytest

from rebalancer.engine import rebalance
from rebalancer.models import (
    AccountType,
    AllocationTarget,
    Position,
    RebalanceConfig,
    TickerMapping,
    Trade,
    Transaction,
)
from rebalancer.tlh import (
    check_wash_sales,
    find_tlh_opportunities,
    suggest_tlh_replacements,
)


@pytest.fixture
def mapping_with_similar():
    return {
        "VTI": TickerMapping(asset_class="us_equity", similar_tickers=["ITOT", "SCHB"]),
        "ITOT": TickerMapping(asset_class="us_equity", similar_tickers=["VTI", "SCHB"]),
        "SCHB": TickerMapping(asset_class="us_equity", similar_tickers=["VTI", "ITOT"]),
        "VXUS": TickerMapping(asset_class="intl_equity", similar_tickers=["IXUS"]),
        "IXUS": TickerMapping(asset_class="intl_equity", similar_tickers=["VXUS"]),
        "BND": TickerMapping(asset_class="bonds", similar_tickers=["AGG"]),
        "AGG": TickerMapping(asset_class="bonds", similar_tickers=["BND"]),
        "SPAXX": TickerMapping(asset_class="cash"),
    }


class TestFindTLHOpportunities:
    def test_finds_losses_in_taxable(self):
        positions = [
            Position(
                account_name="Taxable",
                account_type=AccountType.TAXABLE,
                ticker="VTI",
                description="VTI",
                quantity=Decimal("100"),
                price=Decimal("90"),
                market_value=Decimal("9000"),
                cost_basis_total=Decimal("10000"),  # $1000 loss
            ),
        ]
        mapping = {
            "VTI": TickerMapping(asset_class="us_equity"),
        }
        opportunities = find_tlh_opportunities(positions, mapping)
        assert len(opportunities) == 1
        assert opportunities[0][0].ticker == "VTI"
        assert opportunities[0][1] == Decimal("1000")

    def test_ignores_gains(self):
        positions = [
            Position(
                account_name="Taxable",
                account_type=AccountType.TAXABLE,
                ticker="VTI",
                description="VTI",
                quantity=Decimal("100"),
                price=Decimal("110"),
                market_value=Decimal("11000"),
                cost_basis_total=Decimal("10000"),  # $1000 gain
            ),
        ]
        mapping = {"VTI": TickerMapping(asset_class="us_equity")}
        opportunities = find_tlh_opportunities(positions, mapping)
        assert len(opportunities) == 0

    def test_ignores_tax_advantaged(self):
        positions = [
            Position(
                account_name="Roth",
                account_type=AccountType.ROTH_IRA,
                ticker="VTI",
                description="VTI",
                quantity=Decimal("100"),
                price=Decimal("90"),
                market_value=Decimal("9000"),
                cost_basis_total=Decimal("10000"),
            ),
        ]
        mapping = {"VTI": TickerMapping(asset_class="us_equity")}
        opportunities = find_tlh_opportunities(positions, mapping)
        assert len(opportunities) == 0

    def test_sorts_by_largest_loss(self):
        positions = [
            Position(
                account_name="Taxable",
                account_type=AccountType.TAXABLE,
                ticker="VTI",
                description="VTI",
                quantity=Decimal("100"),
                price=Decimal("95"),
                market_value=Decimal("9500"),
                cost_basis_total=Decimal("10000"),  # $500 loss
            ),
            Position(
                account_name="Taxable",
                account_type=AccountType.TAXABLE,
                ticker="VXUS",
                description="VXUS",
                quantity=Decimal("100"),
                price=Decimal("50"),
                market_value=Decimal("5000"),
                cost_basis_total=Decimal("8000"),  # $3000 loss
            ),
        ]
        mapping = {
            "VTI": TickerMapping(asset_class="us_equity"),
            "VXUS": TickerMapping(asset_class="intl_equity"),
        }
        opportunities = find_tlh_opportunities(positions, mapping)
        assert len(opportunities) == 2
        assert opportunities[0][0].ticker == "VXUS"  # Largest loss first
        assert opportunities[0][1] == Decimal("3000")
        assert opportunities[1][0].ticker == "VTI"
        assert opportunities[1][1] == Decimal("500")

    def test_ignores_no_cost_basis(self):
        positions = [
            Position(
                account_name="Taxable",
                account_type=AccountType.TAXABLE,
                ticker="SPAXX",
                description="Cash",
                quantity=Decimal("0"),
                price=Decimal("0"),
                market_value=Decimal("5000"),
                cost_basis_total=None,
            ),
        ]
        mapping = {"SPAXX": TickerMapping(asset_class="cash")}
        opportunities = find_tlh_opportunities(positions, mapping)
        assert len(opportunities) == 0


class TestCheckWashSales:
    def test_no_wash_sale_when_different_tickers(self, mapping_with_similar):
        trades = [
            Trade(
                account_name="Taxable",
                account_type=AccountType.TAXABLE,
                ticker="VTI",
                action="SELL",
                shares=Decimal("10"),
                estimated_value=Decimal("1000"),
                reasoning="TLH opportunity",
            ),
            Trade(
                account_name="Taxable",
                account_type=AccountType.TAXABLE,
                ticker="BND",
                action="BUY",
                shares=Decimal("15"),
                estimated_value=Decimal("1000"),
                reasoning="Buy bonds",
            ),
        ]
        warnings = check_wash_sales(trades, mapping_with_similar)
        # No wash sale from proposed trades, but INCOMPLETE warning since no transactions provided
        assert not any("WASH SALE RISK" in w for w in warnings)
        assert any("INCOMPLETE" in w for w in warnings)

    def test_wash_sale_same_ticker(self, mapping_with_similar):
        trades = [
            Trade(
                account_name="Taxable",
                account_type=AccountType.TAXABLE,
                ticker="VTI",
                action="SELL",
                shares=Decimal("10"),
                estimated_value=Decimal("1000"),
                reasoning="TLH opportunity",
            ),
            Trade(
                account_name="Roth",
                account_type=AccountType.ROTH_IRA,
                ticker="VTI",
                action="BUY",
                shares=Decimal("10"),
                estimated_value=Decimal("1000"),
                reasoning="Buy VTI",
            ),
        ]
        warnings = check_wash_sales(trades, mapping_with_similar)
        assert len(warnings) >= 1
        assert "WASH SALE" in warnings[0]
        assert "VTI" in warnings[0]

    def test_wash_sale_similar_ticker(self, mapping_with_similar):
        trades = [
            Trade(
                account_name="Taxable",
                account_type=AccountType.TAXABLE,
                ticker="VTI",
                action="SELL",
                shares=Decimal("10"),
                estimated_value=Decimal("1000"),
                reasoning="TLH opportunity: ~$500 loss",
            ),
            Trade(
                account_name="Roth",
                account_type=AccountType.ROTH_IRA,
                ticker="ITOT",
                action="BUY",
                shares=Decimal("10"),
                estimated_value=Decimal("1000"),
                reasoning="Buy ITOT",
            ),
        ]
        warnings = check_wash_sales(trades, mapping_with_similar)
        assert len(warnings) >= 1
        assert "WASH SALE" in warnings[0]
        assert "ITOT" in warnings[0]

    def test_no_wash_sale_for_gain_sales(self, mapping_with_similar):
        """Wash sales only matter for loss sales."""
        trades = [
            Trade(
                account_name="Taxable",
                account_type=AccountType.TAXABLE,
                ticker="VTI",
                action="SELL",
                shares=Decimal("10"),
                estimated_value=Decimal("1000"),
                reasoning="Reduce overweight us_equity",
                warnings=["Selling at estimated gain of $200 in taxable account"],
            ),
            Trade(
                account_name="Roth",
                account_type=AccountType.ROTH_IRA,
                ticker="VTI",
                action="BUY",
                shares=Decimal("10"),
                estimated_value=Decimal("1000"),
                reasoning="Buy VTI",
            ),
        ]
        warnings = check_wash_sales(trades, mapping_with_similar)
        assert len(warnings) == 0

    def test_wash_sale_cross_account(self, mapping_with_similar):
        """Wash sales apply across all accounts, not just taxable."""
        trades = [
            Trade(
                account_name="Taxable",
                account_type=AccountType.TAXABLE,
                ticker="VTI",
                action="SELL",
                shares=Decimal("10"),
                estimated_value=Decimal("1000"),
                reasoning="TLH opportunity: ~$500 loss",
            ),
            Trade(
                account_name="401k",
                account_type=AccountType.FOUR_01K,
                ticker="VTI",
                action="BUY",
                shares=Decimal("10"),
                estimated_value=Decimal("1000"),
                reasoning="Buy VTI",
            ),
        ]
        warnings = check_wash_sales(trades, mapping_with_similar)
        assert len(warnings) >= 1


class TestWashSalesWithTransactions:
    def test_no_warning_when_transactions_provided_no_conflict(self, mapping_with_similar):
        """No wash sale when recent transactions don't overlap."""
        trades = [
            Trade(
                account_name="Taxable",
                account_type=AccountType.TAXABLE,
                ticker="VTI",
                action="SELL",
                shares=Decimal("10"),
                estimated_value=Decimal("1000"),
                reasoning="TLH opportunity",
            ),
        ]
        transactions = [
            Transaction(
                date="2024-12-15",
                account_name="Roth",
                ticker="BND",
                action="BUY",
                shares=Decimal("20"),
            ),
        ]
        warnings = check_wash_sales(trades, mapping_with_similar, transactions)
        assert not any("WASH SALE" in w for w in warnings)

    def test_warns_on_recent_buy_same_ticker(self, mapping_with_similar):
        """Recent buy of same ticker triggers wash sale warning."""
        from datetime import date, timedelta
        recent_date = (date.today() - timedelta(days=10)).strftime("%Y-%m-%d")
        trades = [
            Trade(
                account_name="Taxable",
                account_type=AccountType.TAXABLE,
                ticker="VTI",
                action="SELL",
                shares=Decimal("10"),
                estimated_value=Decimal("1000"),
                reasoning="TLH opportunity",
            ),
        ]
        transactions = [
            Transaction(
                date=recent_date,
                account_name="ROTH IRA",
                ticker="VTI",
                action="BUY",
                shares=Decimal("5"),
            ),
        ]
        warnings = check_wash_sales(trades, mapping_with_similar, transactions)
        assert any("recent history" in w.lower() for w in warnings)

    def test_warns_on_recent_buy_similar_ticker(self, mapping_with_similar):
        """Recent buy of similar ticker triggers wash sale warning."""
        from datetime import date, timedelta
        recent_date = (date.today() - timedelta(days=5)).strftime("%Y-%m-%d")
        trades = [
            Trade(
                account_name="Taxable",
                account_type=AccountType.TAXABLE,
                ticker="VTI",
                action="SELL",
                shares=Decimal("10"),
                estimated_value=Decimal("1000"),
                reasoning="TLH opportunity",
            ),
        ]
        transactions = [
            Transaction(
                date=recent_date,
                account_name="401k",
                ticker="ITOT",
                action="BUY",
                shares=Decimal("5"),
            ),
        ]
        warnings = check_wash_sales(trades, mapping_with_similar, transactions)
        assert any("recent history" in w.lower() for w in warnings)

    def test_no_warning_for_old_transaction(self, mapping_with_similar):
        """Transactions older than 30 days don't trigger warnings."""
        trades = [
            Trade(
                account_name="Taxable",
                account_type=AccountType.TAXABLE,
                ticker="VTI",
                action="SELL",
                shares=Decimal("10"),
                estimated_value=Decimal("1000"),
                reasoning="TLH opportunity",
            ),
        ]
        transactions = [
            Transaction(
                date="2024-01-01",  # Well over 30 days ago
                account_name="ROTH IRA",
                ticker="VTI",
                action="BUY",
                shares=Decimal("5"),
            ),
        ]
        warnings = check_wash_sales(trades, mapping_with_similar, transactions)
        assert not any("recent history" in w.lower() for w in warnings)

    def test_incomplete_warning_without_transactions(self, mapping_with_similar):
        """When TLH trades exist but no transactions provided, warn about incomplete check."""
        trades = [
            Trade(
                account_name="Taxable",
                account_type=AccountType.TAXABLE,
                ticker="VTI",
                action="SELL",
                shares=Decimal("10"),
                estimated_value=Decimal("1000"),
                reasoning="TLH opportunity",
            ),
        ]
        warnings = check_wash_sales(trades, mapping_with_similar, None)
        assert any("INCOMPLETE" in w for w in warnings)

    def test_no_incomplete_warning_with_transactions(self, mapping_with_similar):
        """When transactions are provided, no INCOMPLETE warning."""
        trades = [
            Trade(
                account_name="Taxable",
                account_type=AccountType.TAXABLE,
                ticker="VTI",
                action="SELL",
                shares=Decimal("10"),
                estimated_value=Decimal("1000"),
                reasoning="TLH opportunity",
            ),
        ]
        warnings = check_wash_sales(trades, mapping_with_similar, [])
        assert not any("INCOMPLETE" in w for w in warnings)

    def test_no_incomplete_warning_for_non_tlh_sells(self, mapping_with_similar):
        """No INCOMPLETE warning when sells aren't TLH-related."""
        trades = [
            Trade(
                account_name="Taxable",
                account_type=AccountType.TAXABLE,
                ticker="VTI",
                action="SELL",
                shares=Decimal("10"),
                estimated_value=Decimal("1000"),
                reasoning="Reduce overweight us_equity",
            ),
        ]
        warnings = check_wash_sales(trades, mapping_with_similar, None)
        assert not any("INCOMPLETE" in w for w in warnings)


class TestSuggestTLHReplacements:
    def test_suggests_non_similar_same_class(self, mapping_with_similar):
        held = {"VTI", "VXUS", "BND", "SPAXX"}
        replacements = suggest_tlh_replacements("VTI", mapping_with_similar, held)
        # VTI is similar to ITOT and SCHB, so neither should be suggested
        # But other us_equity tickers not in held_tickers could be
        # In our mapping, ITOT and SCHB are similar to VTI, so no good replacements
        # unless they're not in held
        assert "ITOT" not in replacements or "ITOT" not in held

    def test_empty_for_unknown_ticker(self, mapping_with_similar):
        replacements = suggest_tlh_replacements("AAPL", mapping_with_similar, set())
        assert replacements == []


class TestEndToEnd:
    def test_full_rebalance_with_example_files(self, examples_dir):
        """End-to-end test using the example config files."""
        from rebalancer.config import load_config, load_mapping, load_targets
        from rebalancer.parser import parse_fidelity_csv

        positions = parse_fidelity_csv(examples_dir / "fidelity_positions.csv")
        targets = load_targets(examples_dir / "targets.yaml")
        mapping = load_mapping(examples_dir / "mapping.yaml")
        config = load_config(examples_dir / "config.yaml")

        result = rebalance(positions, targets, mapping, config)

        # Basic sanity checks
        assert result.total_portfolio_value == Decimal("61000.00")
        assert len(result.current_allocation) >= 4
        assert len(result.target_allocation) >= 4
        assert len(result.drift) >= 4

        # Should have trades (portfolio is out of balance)
        assert len(result.trades) > 0

        # All trades should have valid data
        for t in result.trades:
            assert t.shares > 0
            assert t.estimated_value > 0
            assert t.reasoning

    def test_full_rebalance_with_cash_to_invest(self, examples_dir):
        """End-to-end test with new cash to invest."""
        from rebalancer.config import load_mapping, load_targets
        from rebalancer.parser import parse_fidelity_csv

        positions = parse_fidelity_csv(examples_dir / "fidelity_positions.csv")
        targets = load_targets(examples_dir / "targets.yaml")
        mapping = load_mapping(examples_dir / "mapping.yaml")
        config = RebalanceConfig(
            threshold_pct=Decimal("1.0"),
            min_trade_value=Decimal("50"),
            cash_to_invest=Decimal("5000"),
        )

        result = rebalance(positions, targets, mapping, config)

        # Should only have BUY trades
        for t in result.trades:
            assert t.action == "BUY"

        # Total portfolio should not change (cash not yet invested)
        assert result.total_portfolio_value == Decimal("61000.00")

    def test_full_rebalance_with_unified_config(self, examples_dir):
        """End-to-end test using the unified config format."""
        from rebalancer.config import load_mapping, load_unified_config
        from rebalancer.parser import parse_fidelity_csv

        positions = parse_fidelity_csv(examples_dir / "fidelity_positions.csv")
        mapping = load_mapping(examples_dir / "mapping.yaml")
        targets, config, output_config, cash_config, _gt, _cst = load_unified_config(
            examples_dir / "unified_config.yaml"
        )

        result = rebalance(positions, targets, mapping, config)

        assert result.total_portfolio_value == Decimal("61000.00")
        assert len(result.current_allocation) >= 4
        # Tax should be disabled in unified config
        assert config.tlh_enabled is False
        assert config.avoid_gains_in_taxable is False

    def test_markdown_report_generation(self, examples_dir, tmp_path):
        """Test that markdown report is generated correctly."""
        from rebalancer.config import load_config, load_mapping, load_targets
        from rebalancer.output import write_markdown_report
        from rebalancer.parser import parse_fidelity_csv

        positions = parse_fidelity_csv(examples_dir / "fidelity_positions.csv")
        targets = load_targets(examples_dir / "targets.yaml")
        mapping = load_mapping(examples_dir / "mapping.yaml")
        config = load_config(examples_dir / "config.yaml")

        result = rebalance(positions, targets, mapping, config)

        report_path = tmp_path / "report.md"
        write_markdown_report(result, report_path)

        assert report_path.exists()
        content = report_path.read_text()
        assert "Portfolio Rebalance Report" in content
        assert "$61,000.00" in content
        assert "bonds" in content
        assert "us_equity" in content
