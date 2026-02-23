"""Tests for prices.py — fetch_prices() and MONEY_MARKET_TICKERS.

All tests are fully offline: yfinance is patched out at the module boundary so
no network calls are made.
"""
from decimal import Decimal
from unittest.mock import MagicMock, patch

import pytest

from rebalancer.prices import MONEY_MARKET_TICKERS, fetch_prices


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _make_close_df(prices: dict[str, float]):
    """Build a minimal mock of the DataFrame returned by yf.download()."""
    import pandas as pd

    close_data = {ticker: [price] for ticker, price in prices.items()}
    close_df = pd.DataFrame(close_data)

    raw = MagicMock()
    raw.empty = False
    raw.__getitem__ = lambda self, key: close_df  # raw["Close"] → close_df
    return raw


def _make_empty_download():
    raw = MagicMock()
    raw.empty = True
    return raw


# ---------------------------------------------------------------------------
# MONEY_MARKET_TICKERS
# ---------------------------------------------------------------------------

class TestMoneyMarketTickers:
    def test_spaxx_returned_as_one_dollar(self):
        result = fetch_prices(["SPAXX"])
        assert result["SPAXX"] == Decimal("1.0000")

    def test_fdrxx_returned_as_one_dollar(self):
        result = fetch_prices(["FDRXX"])
        assert result["FDRXX"] == Decimal("1.0000")

    def test_all_money_market_tickers_returned(self):
        result = fetch_prices(list(MONEY_MARKET_TICKERS))
        for ticker in MONEY_MARKET_TICKERS:
            assert ticker in result
            assert result[ticker] == Decimal("1.0000")

    def test_money_market_requires_no_network(self):
        """No yfinance calls should be made when all tickers are money-market."""
        with patch("yfinance.download") as mock_dl:
            fetch_prices(["SPAXX", "FDRXX"])
        mock_dl.assert_not_called()


# ---------------------------------------------------------------------------
# Synthetic CASH- tickers
# ---------------------------------------------------------------------------

class TestCashTickers:
    def test_cash_ticker_skipped(self):
        result = fetch_prices(["CASH-USD-INVESTABLE"])
        assert "CASH-USD-INVESTABLE" not in result

    def test_mixed_cash_and_real_skips_only_cash(self):
        close_df = _make_close_df({"VTI": 250.0})
        with patch("yfinance.download", return_value=close_df):
            result = fetch_prices(["CASH-EUR-INVESTABLE", "VTI"])
        assert "CASH-EUR-INVESTABLE" not in result
        assert "VTI" in result

    def test_all_cash_tickers_returns_empty(self):
        result = fetch_prices(["CASH-USD-EMERGENCY", "CASH-EUR-EMERGENCY"])
        assert result == {}


# ---------------------------------------------------------------------------
# Batch download path
# ---------------------------------------------------------------------------

class TestBatchFetch:
    def test_batch_returns_prices(self):
        close_df = _make_close_df({"VTI": 250.12, "VXUS": 60.34})
        with patch("yfinance.download", return_value=close_df):
            result = fetch_prices(["VTI", "VXUS"])
        assert result["VTI"] == Decimal("250.12")
        assert result["VXUS"] == Decimal("60.34")

    def test_batch_result_is_decimal(self):
        close_df = _make_close_df({"BND": 73.456789})
        with patch("yfinance.download", return_value=close_df):
            result = fetch_prices(["BND"])
        assert isinstance(result["BND"], Decimal)

    def test_batch_mixed_with_money_market(self):
        """Money-market tickers should be pre-filled; only real tickers hit yfinance."""
        close_df = _make_close_df({"VTI": 250.0})
        with patch("yfinance.download", return_value=close_df) as mock_dl:
            result = fetch_prices(["SPAXX", "VTI"])
        # yfinance should only be called for VTI, not SPAXX
        called_tickers = mock_dl.call_args[0][0]
        assert "SPAXX" not in called_tickers
        assert result["SPAXX"] == Decimal("1.0000")
        assert result["VTI"] == Decimal("250.0")

    def test_batch_empty_result_triggers_fallback(self):
        """When yf.download returns an empty DataFrame, fall back to individual lookups."""
        mock_ticker = MagicMock()
        mock_ticker.fast_info.last_price = 250.0

        with patch("yfinance.download", return_value=_make_empty_download()):
            with patch("yfinance.Ticker", return_value=mock_ticker):
                result = fetch_prices(["VTI"])

        assert "VTI" in result
        assert result["VTI"] == Decimal("250.0")

    def test_batch_exception_triggers_fallback(self):
        """When yf.download raises, fall back to individual lookups without crashing."""
        mock_ticker = MagicMock()
        mock_ticker.fast_info.last_price = 100.0

        with patch("yfinance.download", side_effect=Exception("network error")):
            with patch("yfinance.Ticker", return_value=mock_ticker):
                result = fetch_prices(["BND"])

        assert "BND" in result

    def test_empty_ticker_list_returns_empty(self):
        with patch("yfinance.download") as mock_dl:
            result = fetch_prices([])
        mock_dl.assert_not_called()
        assert result == {}


# ---------------------------------------------------------------------------
# Individual fallback path
# ---------------------------------------------------------------------------

class TestIndividualFallback:
    def test_individual_fallback_used_when_batch_misses_ticker(self):
        """If yf.download returns data for only some tickers, missing ones use fallback."""
        import pandas as pd

        # Batch only returns VTI; VXUS is missing → should use individual fallback
        close_df_partial = _make_close_df({"VTI": 250.0})

        mock_vxus = MagicMock()
        mock_vxus.fast_info.last_price = 60.0

        def ticker_factory(sym):
            return mock_vxus if sym == "VXUS" else MagicMock()

        with patch("yfinance.download", return_value=close_df_partial):
            with patch("yfinance.Ticker", side_effect=ticker_factory):
                result = fetch_prices(["VTI", "VXUS"])

        assert result["VTI"] == Decimal("250.0")
        assert result["VXUS"] == Decimal("60.0")

    def test_individual_fallback_skips_zero_price(self):
        """A ticker whose fast_info.last_price is 0 should be omitted from results."""
        mock_ticker = MagicMock()
        mock_ticker.fast_info.last_price = 0

        with patch("yfinance.download", return_value=_make_empty_download()):
            with patch("yfinance.Ticker", return_value=mock_ticker):
                result = fetch_prices(["INVALID"])

        assert "INVALID" not in result

    def test_individual_fallback_skips_none_price(self):
        """A ticker whose fast_info.last_price is None should be omitted."""
        mock_ticker = MagicMock()
        mock_ticker.fast_info.last_price = None

        with patch("yfinance.download", return_value=_make_empty_download()):
            with patch("yfinance.Ticker", return_value=mock_ticker):
                result = fetch_prices(["DELISTED"])

        assert "DELISTED" not in result

    def test_individual_fallback_exception_omits_ticker(self):
        """If individual lookup raises, that ticker is omitted (not a crash)."""
        with patch("yfinance.download", return_value=_make_empty_download()):
            with patch("yfinance.Ticker", side_effect=Exception("timeout")):
                result = fetch_prices(["BROKEN"])

        assert "BROKEN" not in result


# ---------------------------------------------------------------------------
# yfinance not installed
# ---------------------------------------------------------------------------

class TestYfinanceNotInstalled:
    def test_returns_empty_when_yfinance_missing(self):
        """If yfinance can't be imported, fetch_prices returns {} rather than crashing."""
        import builtins
        real_import = builtins.__import__

        def mock_import(name, *args, **kwargs):
            if name == "yfinance":
                raise ImportError("No module named 'yfinance'")
            return real_import(name, *args, **kwargs)

        with patch("builtins.__import__", side_effect=mock_import):
            result = fetch_prices(["VTI"])

        assert result == {}
