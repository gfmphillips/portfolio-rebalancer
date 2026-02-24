"""Tests for basket.py — load_basket_csv, compute_basket_orders, basket_template_csv."""
import csv
import io
from decimal import Decimal

import pytest

from rebalancer.basket import (
    basket_template_csv,
    compute_basket_orders,
    load_basket_csv,
)
from rebalancer.models import AccountType, BasketConstituent, InstrumentType, Position, ZERO

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _pos(ticker, value, qty=Decimal("100"), account="Taxable", account_type=AccountType.TAXABLE):
    price = (value / qty).quantize(Decimal("0.01"))
    return Position(
        account_name=account,
        account_type=account_type,
        ticker=ticker,
        description=ticker,
        quantity=qty,
        price=price,
        market_value=value,
    )


SIMPLE_CSV_100 = """\
ticker,target_weight,name,sector,country,is_adr
AAPL,50,Apple Inc.,Technology,US,false
MSFT,30,Microsoft,Technology,US,false
AMZN,20,Amazon,Consumer,US,false
"""

SIMPLE_CSV_FRACTION = """\
ticker,target_weight
AAPL,0.50
MSFT,0.30
AMZN,0.20
"""

SIMPLE_CSV_COMMENTED = """\
ticker,target_weight
# This is a comment
AAPL,7.00
# Another comment
MSFT,3.00
"""


# ---------------------------------------------------------------------------
# load_basket_csv — weight scale detection
# ---------------------------------------------------------------------------

class TestLoadBasketCsv:
    def test_0_to_100_normalized_correctly(self):
        constituents = load_basket_csv(csv_text=SIMPLE_CSV_100)
        w = {c.ticker: c.target_weight for c in constituents}
        assert abs(w["AAPL"] - Decimal("0.5")) < Decimal("0.001")
        assert abs(w["MSFT"] - Decimal("0.3")) < Decimal("0.001")
        assert abs(w["AMZN"] - Decimal("0.2")) < Decimal("0.001")

    def test_0_to_1_accepted_as_is(self):
        constituents = load_basket_csv(csv_text=SIMPLE_CSV_FRACTION)
        w = {c.ticker: c.target_weight for c in constituents}
        # All weights already ≤ 1, already sum to 1 → normalized back to themselves
        assert abs(w["AAPL"] - Decimal("0.5")) < Decimal("0.001")

    def test_weights_sum_to_one(self):
        constituents = load_basket_csv(csv_text=SIMPLE_CSV_100)
        total = sum(c.target_weight for c in constituents)
        assert abs(total - Decimal("1")) < Decimal("0.001")

    def test_comment_lines_skipped(self):
        constituents = load_basket_csv(csv_text=SIMPLE_CSV_COMMENTED)
        assert len(constituents) == 2
        tickers = {c.ticker for c in constituents}
        assert tickers == {"AAPL", "MSFT"}

    def test_duplicate_ticker_raises(self):
        text = "ticker,target_weight\nAAPL,50\nAAPL,30\n"
        with pytest.raises(ValueError, match="Duplicate"):
            load_basket_csv(csv_text=text)

    def test_missing_required_column_raises(self):
        text = "ticker,name\nAAPL,Apple\n"
        with pytest.raises(ValueError, match="target_weight"):
            load_basket_csv(csv_text=text)

    def test_empty_csv_raises(self):
        with pytest.raises(ValueError):
            load_basket_csv(csv_text="ticker,target_weight\n")

    def test_optional_columns_parsed(self):
        constituents = load_basket_csv(csv_text=SIMPLE_CSV_100)
        aapl = next(c for c in constituents if c.ticker == "AAPL")
        assert aapl.name == "Apple Inc."
        assert aapl.sector == "Technology"
        assert aapl.country == "US"
        assert aapl.is_adr is False

    def test_tickers_uppercased(self):
        text = "ticker,target_weight\naapl,50\nmsft,50\n"
        constituents = load_basket_csv(csv_text=text)
        assert all(c.ticker == c.ticker.upper() for c in constituents)

    def test_negative_weight_raises(self):
        text = "ticker,target_weight\nAAPL,-10\nMSFT,50\n"
        with pytest.raises(ValueError, match="Negative"):
            load_basket_csv(csv_text=text)


# ---------------------------------------------------------------------------
# compute_basket_orders — top-up math
# ---------------------------------------------------------------------------

class TestComputeBasketOrders:
    """Test the top-up-underweights algorithm with V+C math."""

    PRICES = {
        "AAPL": Decimal("150.00"),
        "MSFT": Decimal("300.00"),
        "AMZN": Decimal("100.00"),
    }

    def _basket(self):
        return [
            BasketConstituent("AAPL", Decimal("0.50")),
            BasketConstituent("MSFT", Decimal("0.30")),
            BasketConstituent("AMZN", Decimal("0.20")),
        ]

    def test_no_holdings_invests_proportionally(self):
        """With V=0, target = w_i * C → equivalent to plain proportional split."""
        trades = compute_basket_orders(
            constituents=self._basket(),
            current_holdings={},
            equity_cash=Decimal("10000"),
            n_stocks=3,
            min_trade_value=Decimal("50"),
            prices=self.PRICES,
            account_name="Taxable",
            account_type=AccountType.TAXABLE,
        )
        assert len(trades) > 0
        total_cost = sum(t.estimated_value for t in trades)
        assert total_cost <= Decimal("10000")

    def test_existing_holdings_reduce_order(self):
        """With V>0, target = w_i*(V+C); existing holdings deducted → smaller buy."""
        # AAPL: already holds $4000 worth (weight 0.50 * (4000+6000) = 5000 → need 1000 more)
        aapl_held = _pos("AAPL", Decimal("4000"), qty=Decimal("26.666"))
        current = {"AAPL": aapl_held}

        trades_no_holding = compute_basket_orders(
            constituents=self._basket(),
            current_holdings={},
            equity_cash=Decimal("6000"),
            n_stocks=3,
            min_trade_value=Decimal("50"),
            prices=self.PRICES,
            account_name="Taxable",
            account_type=AccountType.TAXABLE,
        )
        trades_with_holding = compute_basket_orders(
            constituents=self._basket(),
            current_holdings=current,
            equity_cash=Decimal("6000"),
            n_stocks=3,
            min_trade_value=Decimal("50"),
            prices=self.PRICES,
            account_name="Taxable",
            account_type=AccountType.TAXABLE,
        )
        cost_no = sum(t.estimated_value for t in trades_no_holding if t.ticker == "AAPL")
        cost_with = sum(t.estimated_value for t in trades_with_holding if t.ticker == "AAPL")
        # Holding AAPL should reduce the buy amount
        assert cost_with < cost_no

    def test_vplus_c_math_prevents_starting_from_zero(self):
        """target_value[i] = w_i*(V+C), not w_i*C — ensures proportional top-up."""
        aapl_held = _pos("AAPL", Decimal("5000"), qty=Decimal("33.333"))
        current = {"AAPL": aapl_held}
        # V=5000 (AAPL only), C=5000 → V+C=10000
        # target AAPL = 0.50 * 10000 = 5000; current=5000 → additional_needed=0
        trades = compute_basket_orders(
            constituents=self._basket(),
            current_holdings=current,
            equity_cash=Decimal("5000"),
            n_stocks=3,
            min_trade_value=Decimal("50"),
            prices=self.PRICES,
            account_name="Taxable",
            account_type=AccountType.TAXABLE,
        )
        # AAPL should need no additional shares (already at target)
        aapl_trades = [t for t in trades if t.ticker == "AAPL"]
        assert aapl_trades == [], "AAPL is already at target weight — should not be bought"

    def test_below_min_trade_value_filtered(self):
        """Orders whose cost < min_trade_value are excluded."""
        trades = compute_basket_orders(
            constituents=[BasketConstituent("AAPL", Decimal("1"))],
            current_holdings={},
            equity_cash=Decimal("10"),   # tiny amount
            n_stocks=1,
            min_trade_value=Decimal("50"),
            prices={"AAPL": Decimal("150")},
            account_name="Taxable",
            account_type=AccountType.TAXABLE,
        )
        assert trades == []

    def test_top_n_limit_respected(self):
        """Only top n_stocks positions are included."""
        basket = [
            BasketConstituent("A", Decimal("0.40")),
            BasketConstituent("B", Decimal("0.30")),
            BasketConstituent("C", Decimal("0.20")),
            BasketConstituent("D", Decimal("0.10")),
        ]
        prices = {t: Decimal("100") for t in "ABCD"}
        trades = compute_basket_orders(
            constituents=basket,
            current_holdings={},
            equity_cash=Decimal("10000"),
            n_stocks=2,
            min_trade_value=Decimal("50"),
            prices=prices,
            account_name="Taxable",
            account_type=AccountType.TAXABLE,
        )
        tickers_bought = {t.ticker for t in trades}
        # Only top 2 by weight: A (0.40), B (0.30)
        assert tickers_bought <= {"A", "B"}

    def test_all_buy_actions(self):
        """All generated trades have action=BUY."""
        trades = compute_basket_orders(
            constituents=self._basket(),
            current_holdings={},
            equity_cash=Decimal("10000"),
            n_stocks=3,
            min_trade_value=Decimal("50"),
            prices=self.PRICES,
            account_name="Taxable",
            account_type=AccountType.TAXABLE,
        )
        assert all(t.action == "BUY" for t in trades)

    def test_missing_price_emits_dollar_allocation(self):
        """Tickers without a price get a dollar-allocation placeholder (shares=0)."""
        trades = compute_basket_orders(
            constituents=self._basket(),
            current_holdings={},
            equity_cash=Decimal("10000"),
            n_stocks=3,
            min_trade_value=Decimal("50"),
            prices={"AAPL": Decimal("150")},  # MSFT and AMZN have no price
            account_name="Taxable",
            account_type=AccountType.TAXABLE,
        )
        tickers = {t.ticker for t in trades}
        # AAPL has price → normal share trade
        assert "AAPL" in tickers
        aapl = next(t for t in trades if t.ticker == "AAPL")
        assert aapl.shares > ZERO
        # MSFT and AMZN have no price → dollar-allocation placeholder rows
        assert "MSFT" in tickers
        assert "AMZN" in tickers
        msft = next(t for t in trades if t.ticker == "MSFT")
        amzn = next(t for t in trades if t.ticker == "AMZN")
        assert msft.shares == ZERO
        assert amzn.shares == ZERO
        assert "No price" in msft.reasoning
        assert "No price" in amzn.reasoning

    def test_empty_basket_returns_empty(self):
        trades = compute_basket_orders(
            constituents=[],
            current_holdings={},
            equity_cash=Decimal("10000"),
            n_stocks=3,
            min_trade_value=Decimal("50"),
            prices=self.PRICES,
            account_name="Taxable",
            account_type=AccountType.TAXABLE,
        )
        assert trades == []

    def test_zero_equity_cash_returns_empty(self):
        trades = compute_basket_orders(
            constituents=self._basket(),
            current_holdings={},
            equity_cash=ZERO,
            n_stocks=3,
            min_trade_value=Decimal("50"),
            prices=self.PRICES,
            account_name="Taxable",
            account_type=AccountType.TAXABLE,
        )
        assert trades == []

    def test_correct_account_type_in_trades(self):
        trades = compute_basket_orders(
            constituents=self._basket(),
            current_holdings={},
            equity_cash=Decimal("10000"),
            n_stocks=3,
            min_trade_value=Decimal("50"),
            prices=self.PRICES,
            account_name="My Taxable",
            account_type=AccountType.TAXABLE,
        )
        for t in trades:
            assert t.account_name == "My Taxable"
            assert t.account_type == AccountType.TAXABLE


# ---------------------------------------------------------------------------
# basket_template_csv
# ---------------------------------------------------------------------------

class TestBasketTemplateCsv:
    def test_is_valid_parseable_csv(self):
        text = basket_template_csv()
        # Strip comment lines before parsing
        non_comment = "\n".join(
            ln for ln in text.splitlines() if not ln.strip().startswith("#")
        )
        reader = csv.DictReader(io.StringIO(non_comment))
        rows = list(reader)
        assert len(rows) > 0

    def test_has_required_columns(self):
        text = basket_template_csv()
        non_comment = "\n".join(
            ln for ln in text.splitlines() if not ln.strip().startswith("#")
        )
        reader = csv.DictReader(io.StringIO(non_comment))
        assert "ticker" in (reader.fieldnames or [])
        assert "target_weight" in (reader.fieldnames or [])

    def test_can_round_trip_through_load(self):
        """Template CSV should be loadable by load_basket_csv."""
        text = basket_template_csv()
        constituents = load_basket_csv(csv_text=text)
        assert len(constituents) > 0
        total_w = sum(c.target_weight for c in constituents)
        assert abs(total_w - Decimal("1")) < Decimal("0.001")
