from decimal import Decimal
from unittest.mock import patch

import pytest

from rebalancer.engine import rebalance
from rebalancer.fx import (
    BankCashAccount,
    _clear_fx_cache,
    convert_bank_cash_to_positions,
    fetch_fx_rate,
)
from rebalancer.models import (
    AccountType,
    AllocationTarget,
    Position,
    RebalanceConfig,
    TickerMapping,
)
from rebalancer.output import _compute_allocation


# ---------------------------------------------------------------------------
# BankCashAccount model validation
# ---------------------------------------------------------------------------


class TestBankCashAccount:
    def test_usd_account(self):
        acct = BankCashAccount(currency="USD", amount=Decimal("1000"), account_name="My Bank")
        assert acct.currency == "USD"
        assert acct.amount == Decimal("1000")

    def test_eur_account(self):
        acct = BankCashAccount(currency="EUR", amount=Decimal("5000"), account_name="EU Bank")
        assert acct.currency == "EUR"

    def test_invalid_currency_rejected(self):
        with pytest.raises(Exception):
            BankCashAccount(currency="GBP", amount=Decimal("100"), account_name="UK Bank")

    def test_negative_amount_accepted(self):
        # Model allows negative, convert_bank_cash_to_positions filters it out
        acct = BankCashAccount(currency="USD", amount=Decimal("-500"), account_name="Bank")
        assert acct.amount == Decimal("-500")


# ---------------------------------------------------------------------------
# convert_bank_cash_to_positions
# ---------------------------------------------------------------------------


class TestConvertBankCash:
    def test_usd_conversion(self):
        accounts = [
            BankCashAccount(currency="USD", amount=Decimal("5000"), account_name="Bank (USD)"),
        ]
        positions = convert_bank_cash_to_positions(accounts, eur_usd_rate=Decimal("1.10"))
        assert len(positions) == 1
        p = positions[0]
        assert p.ticker == "CASH-USD"
        assert p.price == Decimal("1")
        assert p.market_value == Decimal("5000")
        assert p.quantity == Decimal("5000")
        assert p.account_type == AccountType.TAXABLE
        assert p.description == "Bank Cash (USD)"
        assert p.account_name == "Bank (USD)"

    def test_usd_ignores_rate(self):
        """USD conversion should always use price=1, regardless of FX rate."""
        accounts = [
            BankCashAccount(currency="USD", amount=Decimal("1000"), account_name="Bank"),
        ]
        positions = convert_bank_cash_to_positions(accounts, eur_usd_rate=Decimal("999.99"))
        assert positions[0].price == Decimal("1")
        assert positions[0].market_value == Decimal("1000")

    def test_eur_conversion(self):
        accounts = [
            BankCashAccount(currency="EUR", amount=Decimal("2000"), account_name="Bank (EUR)"),
        ]
        rate = Decimal("1.0850")
        positions = convert_bank_cash_to_positions(accounts, eur_usd_rate=rate)
        assert len(positions) == 1
        p = positions[0]
        assert p.ticker == "CASH-EUR"
        assert p.price == rate
        assert p.market_value == Decimal("2170.00")
        assert p.quantity == Decimal("2000")
        assert p.account_type == AccountType.TAXABLE

    def test_eur_conversion_various_rates(self):
        accounts = [
            BankCashAccount(currency="EUR", amount=Decimal("10000"), account_name="Bank"),
        ]
        # Rate = 1.00 (parity)
        pos = convert_bank_cash_to_positions(accounts, Decimal("1.00"))
        assert pos[0].market_value == Decimal("10000.00")

        # Rate = 1.25
        pos = convert_bank_cash_to_positions(accounts, Decimal("1.25"))
        assert pos[0].market_value == Decimal("12500.00")

        # Rate = 0.95 (EUR weaker than USD)
        pos = convert_bank_cash_to_positions(accounts, Decimal("0.95"))
        assert pos[0].market_value == Decimal("9500.00")

    def test_mixed_currencies(self):
        accounts = [
            BankCashAccount(currency="USD", amount=Decimal("3000"), account_name="Bank (USD)"),
            BankCashAccount(currency="EUR", amount=Decimal("1000"), account_name="Bank (EUR)"),
        ]
        rate = Decimal("1.10")
        positions = convert_bank_cash_to_positions(accounts, eur_usd_rate=rate)
        assert len(positions) == 2
        usd_pos = [p for p in positions if p.ticker == "CASH-USD"][0]
        eur_pos = [p for p in positions if p.ticker == "CASH-EUR"][0]
        assert usd_pos.market_value == Decimal("3000")
        assert eur_pos.market_value == Decimal("1100.00")

    def test_zero_amount_skipped(self):
        accounts = [
            BankCashAccount(currency="USD", amount=Decimal("0"), account_name="Empty"),
        ]
        positions = convert_bank_cash_to_positions(accounts, eur_usd_rate=Decimal("1.10"))
        assert len(positions) == 0

    def test_negative_amount_skipped(self):
        accounts = [
            BankCashAccount(currency="USD", amount=Decimal("-500"), account_name="Negative"),
        ]
        positions = convert_bank_cash_to_positions(accounts, eur_usd_rate=Decimal("1.10"))
        assert len(positions) == 0

    def test_empty_accounts(self):
        positions = convert_bank_cash_to_positions([], eur_usd_rate=Decimal("1.10"))
        assert positions == []

    def test_cost_basis_is_none(self):
        accounts = [
            BankCashAccount(currency="USD", amount=Decimal("100"), account_name="Bank"),
        ]
        positions = convert_bank_cash_to_positions(accounts, eur_usd_rate=Decimal("1.10"))
        assert positions[0].cost_basis_total is None

    def test_eur_market_value_quantized_to_cents(self):
        """EUR conversion should round market_value to 2 decimal places."""
        accounts = [
            BankCashAccount(currency="EUR", amount=Decimal("333"), account_name="Bank"),
        ]
        # 333 * 1.0850 = 361.305 -> should quantize to 361.31 or 361.30
        pos = convert_bank_cash_to_positions(accounts, Decimal("1.0850"))
        # Check it has exactly 2 decimal places
        assert pos[0].market_value == pos[0].market_value.quantize(Decimal("0.01"))

    def test_large_amounts(self):
        accounts = [
            BankCashAccount(currency="EUR", amount=Decimal("1000000"), account_name="Big Bank"),
        ]
        pos = convert_bank_cash_to_positions(accounts, Decimal("1.18"))
        assert pos[0].market_value == Decimal("1180000.00")

    def test_float_derived_rate(self):
        """Simulate what happens when rate comes from Streamlit float number_input."""
        float_rate = 1.10
        rate = Decimal(str(float_rate))
        accounts = [
            BankCashAccount(currency="EUR", amount=Decimal("5000"), account_name="Bank"),
        ]
        pos = convert_bank_cash_to_positions(accounts, rate)
        assert pos[0].market_value == Decimal("5500.00")


# ---------------------------------------------------------------------------
# fetch_fx_rate
# ---------------------------------------------------------------------------


class TestFetchFxRate:
    def setup_method(self):
        _clear_fx_cache()

    def test_returns_decimal_on_success(self):
        mock_json = {"rates": {"USD": 1.18}, "base": "EUR", "date": "2025-01-01"}
        with patch("rebalancer.fx.requests.get") as mock_get:
            mock_get.return_value.status_code = 200
            mock_get.return_value.json.return_value = mock_json
            mock_get.return_value.raise_for_status = lambda: None
            rate = fetch_fx_rate("EUR", "USD")
        assert rate == Decimal("1.18")

    def test_returns_none_on_network_error(self):
        with patch("rebalancer.fx.requests.get", side_effect=Exception("timeout")):
            rate = fetch_fx_rate("EUR", "USD")
        assert rate is None

    def test_returns_none_on_bad_json(self):
        with patch("rebalancer.fx.requests.get") as mock_get:
            mock_get.return_value.raise_for_status = lambda: None
            mock_get.return_value.json.return_value = {"error": "bad request"}
            rate = fetch_fx_rate("EUR", "USD")
        assert rate is None

    def test_caches_result(self):
        mock_json = {"rates": {"USD": 1.18}, "base": "EUR", "date": "2025-01-01"}
        with patch("rebalancer.fx.requests.get") as mock_get:
            mock_get.return_value.status_code = 200
            mock_get.return_value.json.return_value = mock_json
            mock_get.return_value.raise_for_status = lambda: None
            rate1 = fetch_fx_rate("EUR", "USD")
            rate2 = fetch_fx_rate("EUR", "USD")
            # Should only call the API once due to caching
            assert mock_get.call_count == 1
        assert rate1 == rate2

    def test_calls_frankfurter_api(self):
        mock_json = {"rates": {"USD": 1.18}, "base": "EUR", "date": "2025-01-01"}
        with patch("rebalancer.fx.requests.get") as mock_get:
            mock_get.return_value.status_code = 200
            mock_get.return_value.json.return_value = mock_json
            mock_get.return_value.raise_for_status = lambda: None
            fetch_fx_rate("EUR", "USD")
            call_args = mock_get.call_args
            assert "frankfurter.app" in call_args[0][0]
            assert call_args[1]["params"] == {"from": "EUR", "to": "USD"}


# ---------------------------------------------------------------------------
# Integration: bank positions + _compute_allocation
# ---------------------------------------------------------------------------


@pytest.fixture
def brokerage_positions():
    """Realistic brokerage positions for integration tests."""
    return [
        Position(
            account_name="Individual",
            account_type=AccountType.TAXABLE,
            ticker="VTI",
            description="VANGUARD TOTAL STOCK MKT ETF",
            quantity=Decimal("100"),
            price=Decimal("250.00"),
            market_value=Decimal("25000.00"),
            cost_basis_total=Decimal("20000.00"),
        ),
        Position(
            account_name="Individual",
            account_type=AccountType.TAXABLE,
            ticker="VXUS",
            description="VANGUARD TOTAL INTL STOCK ETF",
            quantity=Decimal("100"),
            price=Decimal("60.00"),
            market_value=Decimal("6000.00"),
            cost_basis_total=Decimal("5000.00"),
        ),
        Position(
            account_name="Individual",
            account_type=AccountType.TAXABLE,
            ticker="BND",
            description="VANGUARD TOTAL BOND MKT ETF",
            quantity=Decimal("50"),
            price=Decimal("72.00"),
            market_value=Decimal("3600.00"),
            cost_basis_total=Decimal("4000.00"),
        ),
        Position(
            account_name="Individual",
            account_type=AccountType.TAXABLE,
            ticker="SPAXX",
            description="FIDELITY GOVERNMENT MONEY MARKET",
            quantity=Decimal("0"),
            price=Decimal("0"),
            market_value=Decimal("2400.00"),
            cost_basis_total=None,
        ),
    ]


@pytest.fixture
def base_mapping():
    return {
        "VTI": TickerMapping(asset_class="us_equity"),
        "VXUS": TickerMapping(asset_class="intl_equity"),
        "BND": TickerMapping(asset_class="bonds"),
        "SPAXX": TickerMapping(asset_class="cash"),
    }


class TestAllocationWithBankCash:
    def test_overview_total_includes_bank_cash(self, brokerage_positions, base_mapping):
        """Total portfolio value should include bank cash holdings."""
        # Brokerage total: 25000 + 6000 + 3600 + 2400 = 37000
        total_without, _, _ = _compute_allocation(brokerage_positions, base_mapping)
        assert total_without == Decimal("37000.00")

        # Add USD bank cash
        bank_accounts = [
            BankCashAccount(currency="USD", amount=Decimal("10000"), account_name="Bank (USD)"),
        ]
        bank_pos = convert_bank_cash_to_positions(bank_accounts, Decimal("1.10"))
        mapping = {**base_mapping, "CASH-USD": TickerMapping(asset_class="cash")}

        all_pos = brokerage_positions + bank_pos
        total_with, _, _ = _compute_allocation(all_pos, mapping)
        assert total_with == Decimal("47000.00")

    def test_overview_total_includes_eur_bank_cash(self, brokerage_positions, base_mapping):
        """EUR bank cash should be converted to USD and added to total."""
        bank_accounts = [
            BankCashAccount(currency="EUR", amount=Decimal("5000"), account_name="Bank (EUR)"),
        ]
        rate = Decimal("1.18")
        bank_pos = convert_bank_cash_to_positions(bank_accounts, rate)
        mapping = {**base_mapping, "CASH-EUR": TickerMapping(asset_class="cash")}

        all_pos = brokerage_positions + bank_pos
        total, _, _ = _compute_allocation(all_pos, mapping)
        # 37000 + 5000*1.18 = 37000 + 5900 = 42900
        assert total == Decimal("42900.00")

    def test_cash_percentage_increases_with_bank_cash(self, brokerage_positions, base_mapping):
        """Adding bank cash should increase the cash allocation percentage."""
        _, _, pct_without = _compute_allocation(brokerage_positions, base_mapping)
        cash_pct_before = pct_without["cash"]

        bank_accounts = [
            BankCashAccount(currency="USD", amount=Decimal("20000"), account_name="Bank (USD)"),
        ]
        bank_pos = convert_bank_cash_to_positions(bank_accounts, Decimal("1.10"))
        mapping = {**base_mapping, "CASH-USD": TickerMapping(asset_class="cash")}

        all_pos = brokerage_positions + bank_pos
        _, _, pct_with = _compute_allocation(all_pos, mapping)
        cash_pct_after = pct_with["cash"]

        assert cash_pct_after > cash_pct_before

    def test_non_cash_percentages_decrease_with_bank_cash(self, brokerage_positions, base_mapping):
        """Adding bank cash should decrease non-cash allocation percentages."""
        _, _, pct_without = _compute_allocation(brokerage_positions, base_mapping)

        bank_accounts = [
            BankCashAccount(currency="USD", amount=Decimal("20000"), account_name="Bank (USD)"),
        ]
        bank_pos = convert_bank_cash_to_positions(bank_accounts, Decimal("1.10"))
        mapping = {**base_mapping, "CASH-USD": TickerMapping(asset_class="cash")}

        all_pos = brokerage_positions + bank_pos
        _, _, pct_with = _compute_allocation(all_pos, mapping)

        assert pct_with["us_equity"] < pct_without["us_equity"]
        assert pct_with["intl_equity"] < pct_without["intl_equity"]
        assert pct_with["bonds"] < pct_without["bonds"]

    def test_all_percentages_sum_to_100(self, brokerage_positions, base_mapping):
        """Percentages should sum to ~100 even with bank cash added."""
        bank_accounts = [
            BankCashAccount(currency="USD", amount=Decimal("10000"), account_name="Bank (USD)"),
            BankCashAccount(currency="EUR", amount=Decimal("5000"), account_name="Bank (EUR)"),
        ]
        bank_pos = convert_bank_cash_to_positions(bank_accounts, Decimal("1.10"))
        mapping = {
            **base_mapping,
            "CASH-USD": TickerMapping(asset_class="cash"),
            "CASH-EUR": TickerMapping(asset_class="cash"),
        }

        all_pos = brokerage_positions + bank_pos
        _, _, pct = _compute_allocation(all_pos, mapping)
        total_pct = sum(pct.values())
        # Rounding can cause minor deviation from exactly 100
        assert Decimal("99.98") <= total_pct <= Decimal("100.02")

    def test_bank_cash_appears_in_value_by_class(self, brokerage_positions, base_mapping):
        """Bank cash should be reflected in the value_by_class dict."""
        _, value_without, _ = _compute_allocation(brokerage_positions, base_mapping)
        cash_value_before = value_without["cash"]

        bank_accounts = [
            BankCashAccount(currency="USD", amount=Decimal("8000"), account_name="Bank (USD)"),
        ]
        bank_pos = convert_bank_cash_to_positions(bank_accounts, Decimal("1.10"))
        mapping = {**base_mapping, "CASH-USD": TickerMapping(asset_class="cash")}

        all_pos = brokerage_positions + bank_pos
        _, value_with, _ = _compute_allocation(all_pos, mapping)

        assert value_with["cash"] == cash_value_before + Decimal("8000")

    def test_mixed_usd_eur_bank_cash_values(self, brokerage_positions, base_mapping):
        """Both USD and EUR bank cash should contribute to cash allocation."""
        bank_accounts = [
            BankCashAccount(currency="USD", amount=Decimal("5000"), account_name="Bank (USD)"),
            BankCashAccount(currency="EUR", amount=Decimal("3000"), account_name="Bank (EUR)"),
        ]
        rate = Decimal("1.10")
        bank_pos = convert_bank_cash_to_positions(bank_accounts, rate)
        mapping = {
            **base_mapping,
            "CASH-USD": TickerMapping(asset_class="cash"),
            "CASH-EUR": TickerMapping(asset_class="cash"),
        }

        all_pos = brokerage_positions + bank_pos
        total, value_by_class, _ = _compute_allocation(all_pos, mapping)

        # Bank cash: 5000 USD + 3000*1.10 EUR = 5000 + 3300 = 8300
        # Brokerage cash (SPAXX): 2400
        assert value_by_class["cash"] == Decimal("2400.00") + Decimal("5000") + Decimal("3300.00")
        # Total: 37000 + 8300 = 45300
        assert total == Decimal("45300.00")


# ---------------------------------------------------------------------------
# Integration: rebalance engine ignores bank positions
# ---------------------------------------------------------------------------


class TestRebalanceIgnoresBankCash:
    def test_rebalance_total_excludes_bank_cash(self, brokerage_positions, base_mapping):
        """Rebalance engine should only see brokerage positions, not bank cash."""
        targets = [
            AllocationTarget(asset_class="us_equity", target_pct=Decimal("60")),
            AllocationTarget(asset_class="intl_equity", target_pct=Decimal("20")),
            AllocationTarget(asset_class="bonds", target_pct=Decimal("15")),
            AllocationTarget(asset_class="cash", target_pct=Decimal("5")),
        ]
        config = RebalanceConfig()

        # Add bank cash tickers to mapping (as web.py does)
        mapping = {
            **base_mapping,
            "CASH-USD": TickerMapping(asset_class="cash"),
            "CASH-EUR": TickerMapping(asset_class="cash"),
        }

        # Rebalance with ONLY brokerage positions (as web.py does on line 338)
        result = rebalance(brokerage_positions, targets, mapping, config)
        assert result.total_portfolio_value == Decimal("37000.00")

    def test_rebalance_trades_dont_reference_bank_tickers(self, brokerage_positions, base_mapping):
        """No trade should reference CASH-USD or CASH-EUR synthetic tickers."""
        targets = [
            AllocationTarget(asset_class="us_equity", target_pct=Decimal("60")),
            AllocationTarget(asset_class="intl_equity", target_pct=Decimal("20")),
            AllocationTarget(asset_class="bonds", target_pct=Decimal("15")),
            AllocationTarget(asset_class="cash", target_pct=Decimal("5")),
        ]
        config = RebalanceConfig(threshold_pct=Decimal("1"))

        mapping = {
            **base_mapping,
            "CASH-USD": TickerMapping(asset_class="cash"),
            "CASH-EUR": TickerMapping(asset_class="cash"),
        }

        result = rebalance(brokerage_positions, targets, mapping, config)
        trade_tickers = {t.ticker for t in result.trades}
        assert "CASH-USD" not in trade_tickers
        assert "CASH-EUR" not in trade_tickers

    def test_rebalance_allocations_based_on_brokerage_only(
        self, brokerage_positions, base_mapping
    ):
        """Rebalance allocations should be based on brokerage portfolio only."""
        targets = [
            AllocationTarget(asset_class="us_equity", target_pct=Decimal("60")),
            AllocationTarget(asset_class="intl_equity", target_pct=Decimal("20")),
            AllocationTarget(asset_class="bonds", target_pct=Decimal("15")),
            AllocationTarget(asset_class="cash", target_pct=Decimal("5")),
        ]
        config = RebalanceConfig()

        # Without bank tickers in mapping
        result_clean = rebalance(brokerage_positions, targets, base_mapping, config)

        # With bank tickers in mapping
        mapping_with_bank = {
            **base_mapping,
            "CASH-USD": TickerMapping(asset_class="cash"),
            "CASH-EUR": TickerMapping(asset_class="cash"),
        }
        result_with_bank_mapping = rebalance(
            brokerage_positions, targets, mapping_with_bank, config
        )

        # Allocations should be identical since only brokerage positions are used
        assert result_clean.current_allocation == result_with_bank_mapping.current_allocation
        assert result_clean.total_portfolio_value == result_with_bank_mapping.total_portfolio_value
