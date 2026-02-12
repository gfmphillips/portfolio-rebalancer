from decimal import Decimal

import pytest

from rebalancer.models import AccountType, RebalanceResult, SortKey, TaxImpact, Trade
from rebalancer.output import _format_currency, _format_pct, filter_actionable_trades, sort_trades, write_markdown_report


def _make_trade(action="BUY", ticker="VTI", value=1000, account="Acct1"):
    return Trade(
        account_name=account,
        account_type=AccountType.TAXABLE,
        ticker=ticker,
        action=action,
        shares=Decimal("10"),
        estimated_value=Decimal(str(value)),
        reasoning="test",
    )


class TestFormatCurrency:
    def test_precision_zero(self):
        assert _format_currency(Decimal("1234.56"), precision=0) == "$1,235"

    def test_precision_two(self):
        assert _format_currency(Decimal("1234.56"), precision=2) == "$1,234.56"

    def test_large_number(self):
        assert _format_currency(Decimal("1234567.89"), precision=0) == "$1,234,568"

    def test_zero(self):
        assert _format_currency(Decimal("0"), precision=0) == "$0"

    def test_default_precision(self):
        assert _format_currency(Decimal("1234.56")) == "$1,235"


class TestFormatPct:
    def test_default_precision(self):
        assert _format_pct(Decimal("12.3456")) == "12.35%"

    def test_precision_one(self):
        assert _format_pct(Decimal("12.3456"), precision=1) == "12.3%"

    def test_precision_zero(self):
        assert _format_pct(Decimal("12.3456"), precision=0) == "12%"


class TestSortTrades:
    def test_sells_first(self):
        trades = [
            _make_trade("BUY", "VTI", 500),
            _make_trade("SELL", "BND", 300),
            _make_trade("BUY", "VXUS", 200),
        ]
        result = sort_trades(trades, [SortKey.SELLS_FIRST])
        assert result[0].action == "SELL"

    def test_buys_first(self):
        trades = [
            _make_trade("SELL", "VTI", 500),
            _make_trade("BUY", "BND", 300),
        ]
        result = sort_trades(trades, [SortKey.BUYS_FIRST])
        assert result[0].action == "BUY"

    def test_largest_first(self):
        trades = [
            _make_trade("BUY", "VTI", 100),
            _make_trade("BUY", "BND", 5000),
            _make_trade("BUY", "VXUS", 500),
        ]
        result = sort_trades(trades, [SortKey.LARGEST_TRADE_FIRST])
        assert result[0].estimated_value == Decimal("5000")
        assert result[-1].estimated_value == Decimal("100")

    def test_smallest_first(self):
        trades = [
            _make_trade("BUY", "VTI", 5000),
            _make_trade("BUY", "BND", 100),
        ]
        result = sort_trades(trades, [SortKey.SMALLEST_TRADE_FIRST])
        assert result[0].estimated_value == Decimal("100")

    def test_by_account(self):
        trades = [
            _make_trade("BUY", "VTI", 500, account="Zebra"),
            _make_trade("BUY", "BND", 500, account="Alpha"),
        ]
        result = sort_trades(trades, [SortKey.BY_ACCOUNT])
        assert result[0].account_name == "Alpha"

    def test_by_ticker(self):
        trades = [
            _make_trade("BUY", "VXUS", 500),
            _make_trade("BUY", "BND", 500),
        ]
        result = sort_trades(trades, [SortKey.BY_TICKER])
        assert result[0].ticker == "BND"

    def test_multi_key_sells_first_largest_first(self):
        trades = [
            _make_trade("BUY", "VTI", 5000),
            _make_trade("SELL", "BND", 100),
            _make_trade("SELL", "VXUS", 3000),
            _make_trade("BUY", "FXAIX", 200),
        ]
        result = sort_trades(trades, [SortKey.SELLS_FIRST, SortKey.LARGEST_TRADE_FIRST])
        # Sells should come first, and within sells, largest first
        assert result[0].action == "SELL"
        assert result[0].estimated_value == Decimal("3000")
        assert result[1].action == "SELL"
        assert result[1].estimated_value == Decimal("100")

    def test_empty_trades(self):
        assert sort_trades([], [SortKey.SELLS_FIRST]) == []


class TestFilterActionableTrades:
    def test_filters_below_threshold(self):
        trades = [
            _make_trade("BUY", "VTI", 1000),
            _make_trade("BUY", "BND", 10),
        ]
        result = filter_actionable_trades(trades, Decimal("500"), show_only=True)
        assert len(result) == 1
        assert result[0].ticker == "VTI"

    def test_keeps_above_threshold(self):
        trades = [_make_trade("BUY", "VTI", 1000)]
        result = filter_actionable_trades(trades, Decimal("500"), show_only=True)
        assert len(result) == 1

    def test_disabled_returns_all(self):
        trades = [
            _make_trade("BUY", "VTI", 1000),
            _make_trade("BUY", "BND", 10),
        ]
        result = filter_actionable_trades(trades, Decimal("500"), show_only=False)
        assert len(result) == 2

    def test_exact_threshold(self):
        trades = [_make_trade("BUY", "VTI", 500)]
        result = filter_actionable_trades(trades, Decimal("500"), show_only=True)
        assert len(result) == 1

    def test_empty_trades(self):
        result = filter_actionable_trades([], Decimal("500"), show_only=True)
        assert result == []


class TestLotInfoInOutput:
    def _make_lot_trade(self, lot_date=None, gain_loss=None):
        return Trade(
            account_name="Taxable",
            account_type=AccountType.TAXABLE,
            ticker="VTI",
            action="SELL",
            shares=Decimal("10"),
            estimated_value=Decimal("2500"),
            reasoning=f"Reduce overweight us_equity (lot: {lot_date}, HIFO)" if lot_date else "Reduce overweight us_equity",
            estimated_gain_loss=gain_loss,
            lot_acquisition_date=lot_date,
        )

    def test_lot_specific_label_in_markdown(self, tmp_path):
        trade = self._make_lot_trade("2020-03-15", Decimal("500"))
        result = RebalanceResult(
            total_portfolio_value=Decimal("100000"),
            current_allocation={"us_equity": Decimal("70")},
            target_allocation={"us_equity": Decimal("60")},
            drift={"us_equity": Decimal("10")},
            trades=[trade],
            warnings=[],
            tax_impact=TaxImpact(
                estimated_total_gains=Decimal("500"),
                estimated_total_losses=Decimal("0"),
                estimated_net=Decimal("500"),
                taxable_trades_count=1,
            ),
        )
        path = tmp_path / "report.md"
        write_markdown_report(result, path)
        content = path.read_text()
        assert "(lot-specific)" in content
        assert "(approximate)" not in content

    def test_approximate_label_when_no_lots(self, tmp_path):
        trade = self._make_lot_trade(None, Decimal("500"))
        result = RebalanceResult(
            total_portfolio_value=Decimal("100000"),
            current_allocation={"us_equity": Decimal("70")},
            target_allocation={"us_equity": Decimal("60")},
            drift={"us_equity": Decimal("10")},
            trades=[trade],
            warnings=[],
            tax_impact=TaxImpact(
                estimated_total_gains=Decimal("500"),
                estimated_total_losses=Decimal("0"),
                estimated_net=Decimal("500"),
                taxable_trades_count=1,
            ),
        )
        path = tmp_path / "report.md"
        write_markdown_report(result, path)
        content = path.read_text()
        assert "(approximate)" in content

    def test_mixed_label(self, tmp_path):
        lot_trade = self._make_lot_trade("2020-03-15", Decimal("500"))
        blended_trade = self._make_lot_trade(None, Decimal("200"))
        result = RebalanceResult(
            total_portfolio_value=Decimal("100000"),
            current_allocation={"us_equity": Decimal("70")},
            target_allocation={"us_equity": Decimal("60")},
            drift={"us_equity": Decimal("10")},
            trades=[lot_trade, blended_trade],
            warnings=[],
            tax_impact=TaxImpact(
                estimated_total_gains=Decimal("700"),
                estimated_total_losses=Decimal("0"),
                estimated_net=Decimal("700"),
                taxable_trades_count=2,
            ),
        )
        path = tmp_path / "report.md"
        write_markdown_report(result, path)
        content = path.read_text()
        assert "mixed" in content

    def test_lot_date_in_reasoning(self):
        trade = self._make_lot_trade("2020-03-15", Decimal("500"))
        assert "lot: 2020-03-15" in trade.reasoning
