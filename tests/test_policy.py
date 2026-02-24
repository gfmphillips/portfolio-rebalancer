"""Acceptance tests for the policy-aware engine.

These tests exercise new_money_plan() end-to-end and the policy.py helpers.
No network calls, no Streamlit.
"""
from decimal import Decimal

import pytest

from rebalancer.engine import compute_allocation_views, new_money_plan
from rebalancer.models import (
    AccountType,
    AllocationView,
    BasketConstituent,
    BLOCKED_BUY_TYPES,
    BuyPlan,
    DefensiveMode,
    InstrumentType,
    PolicyConfig,
    Position,
    TickerMapping,
    ZERO,
)
from rebalancer.policy import (
    build_legacy_sell_flags,
    is_account_buy_enabled,
    is_etf_buy_blocked,
    months_to_reenter_band,
    should_recommend_legacy_sell,
)

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _pos(ticker, value, account="Taxable", account_type=AccountType.TAXABLE):
    return Position(
        account_name=account,
        account_type=account_type,
        ticker=ticker,
        description=ticker,
        quantity=Decimal("100"),
        price=(value / Decimal("100")).quantize(Decimal("0.01")),
        market_value=value,
    )


def _mapping(asset_class="us_equity", instrument_type=InstrumentType.legacy_fund_or_etf, never_want=False):
    return TickerMapping(
        asset_class=asset_class,
        instrument_type=instrument_type,
        never_want=never_want,
    )


def _policy(**kwargs) -> PolicyConfig:
    defaults = dict(
        target_stock_pct=Decimal("0.80"),
        target_bond_pct=Decimal("0.20"),
        investable_cash_eur=Decimal("10000"),
        eurusd_fx=Decimal("1.10"),
        rebalance_band_abs=Decimal("0.05"),
        buy_enabled_account_types=frozenset({"taxable"}),
        allow_legacy_etf_sales=False,
        monthly_investable_cash_eur=Decimal("0"),
        defensive_mode=DefensiveMode.treasury_only,
    )
    defaults.update(kwargs)
    return PolicyConfig(**defaults)


# ---------------------------------------------------------------------------
# TestWithinBandWithCash
# ---------------------------------------------------------------------------

class TestWithinBandWithCash:
    """Portfolio is at exactly 80/20 — within ±5% band."""

    def _plan(self):
        positions = [
            _pos("VTI", Decimal("80000")),  # stock
            _pos("BND", Decimal("20000")),  # bonds
        ]
        mapping = {
            "VTI": _mapping("us_equity"),
            "BND": _mapping("bonds"),
        }
        policy = _policy(investable_cash_eur=Decimal("10000"))
        return new_money_plan(positions, policy, mapping)

    def test_both_cash_pools_nonzero(self):
        plan = self._plan()
        assert plan.equity_cash_usd > 0
        assert plan.defensive_cash_usd > 0

    def test_no_etf_buys(self):
        plan = self._plan()
        all_trades = plan.equity_instructions + plan.defensive_instructions
        for t in all_trades:
            if t.action == "BUY":
                # No blocked-type instruments should be purchased
                # (placeholder rows with ticker="TREASURY" etc. are fine)
                assert t.ticker in {
                    "TREASURY", "CD", "US_STOCK_BASKET"
                } or t.ticker.startswith("TREASURY_"), (
                    f"Unexpected BUY of potentially blocked ticker: {t.ticker}"
                )

    def test_no_legacy_sell_trades(self):
        plan = self._plan()
        assert plan.legacy_sell_trades == []

    def test_equity_and_defensive_sum_to_total(self):
        plan = self._plan()
        total = plan.investable_cash_usd
        assert plan.equity_cash_usd + plan.defensive_cash_usd == total


# ---------------------------------------------------------------------------
# TestOverweightStocks
# ---------------------------------------------------------------------------

class TestOverweightStocks:
    """Portfolio is 87% stocks / 13% bonds — outside upper band (80+5=85)."""

    def _plan(self, defensive_mode=DefensiveMode.treasury_only, **mode_kwargs):
        positions = [
            _pos("VTI", Decimal("87000")),
            _pos("BND", Decimal("13000")),
        ]
        mapping = {
            "VTI": _mapping("us_equity"),
            "BND": _mapping("bonds"),
        }
        policy = _policy(
            investable_cash_eur=Decimal("10000"),
            defensive_mode=defensive_mode,
            **mode_kwargs,
        )
        return new_money_plan(positions, policy, mapping)

    def test_equity_cash_is_zero(self):
        plan = self._plan()
        assert plan.equity_cash_usd == ZERO

    def test_defensive_cash_is_full_investable(self):
        plan = self._plan()
        assert plan.defensive_cash_usd == plan.investable_cash_usd

    def test_single_treasury_instruction_default(self):
        plan = self._plan()
        assert len(plan.defensive_instructions) == 1
        assert plan.defensive_instructions[0].ticker == "TREASURY"

    def test_defensive_instruction_amount_from_defensive_cash_not_equity(self):
        """Bug-regression: placeholder must come from defensive_cash_usd, not equity_cash."""
        plan = self._plan()
        assert plan.equity_cash_usd == ZERO, "Pre-condition: equity_cash must be 0"
        total_def = sum(t.estimated_value for t in plan.defensive_instructions)
        assert total_def == plan.defensive_cash_usd
        assert total_def > ZERO

    def test_treasury_cd_split_mode(self):
        plan = self._plan(
            defensive_mode=DefensiveMode.treasury_cd_split,
            treasury_pct=Decimal("0.70"),
            cd_pct=Decimal("0.30"),
        )
        tickers = {t.ticker for t in plan.defensive_instructions}
        assert "TREASURY" in tickers
        assert "CD" in tickers

    def test_ladder_mode_produces_multiple_rows(self):
        plan = self._plan(
            defensive_mode=DefensiveMode.ladder,
            ladder_rungs_months=[6, 12, 24, 36],
            ladder_currency="EUR",
        )
        assert len(plan.defensive_instructions) == 4
        assert all("TREASURY_" in t.ticker for t in plan.defensive_instructions)

    def test_band_detection_from_total_view(self):
        """outside_band must be True even though implementable has same proportions."""
        plan = self._plan()
        assert not plan.total_view.within_bands


# ---------------------------------------------------------------------------
# TestUnderweightStocks
# ---------------------------------------------------------------------------

class TestUnderweightStocks:
    """Portfolio is 73% stocks / 27% bonds — outside lower band (80-5=75)."""

    def _basket(self):
        return [
            BasketConstituent("AAPL", Decimal("0.50")),
            BasketConstituent("MSFT", Decimal("0.30")),
            BasketConstituent("AMZN", Decimal("0.20")),
        ]

    def _plan(self):
        positions = [
            _pos("VTI", Decimal("73000")),
            _pos("BND", Decimal("27000")),
        ]
        mapping = {
            "VTI": _mapping("us_equity"),
            "BND": _mapping("bonds"),
        }
        prices = {
            "AAPL": Decimal("150.00"),
            "MSFT": Decimal("300.00"),
            "AMZN": Decimal("100.00"),
        }
        policy = _policy(investable_cash_eur=Decimal("10000"), basket_size=3)
        return new_money_plan(positions, policy, mapping, basket=self._basket(), prices=prices)

    def test_defensive_cash_is_zero(self):
        plan = self._plan()
        assert plan.defensive_cash_usd == ZERO

    def test_equity_cash_is_full_investable(self):
        plan = self._plan()
        assert plan.equity_cash_usd == plan.investable_cash_usd

    def test_equity_instructions_produced(self):
        plan = self._plan()
        assert len(plan.equity_instructions) > 0

    def test_no_legacy_sell_trades(self):
        plan = self._plan()
        assert plan.legacy_sell_trades == []


# ---------------------------------------------------------------------------
# TestOutsideBandInsufficientCash
# ---------------------------------------------------------------------------

class TestOutsideBandInsufficientCash:
    """Portfolio 87/13; only €500/month; horizon=18 months → months_to_fix >> 18."""

    def _positions_and_mapping(self):
        positions = [
            _pos("VTI", Decimal("870000")),
            _pos("BND", Decimal("130000")),
        ]
        mapping = {
            "VTI": _mapping("us_equity", instrument_type=InstrumentType.legacy_fund_or_etf, never_want=False),
            "BND": _mapping("bonds", instrument_type=InstrumentType.legacy_fund_or_etf),
        }
        return positions, mapping

    def test_months_to_fix_exceeds_horizon(self):
        positions, mapping = self._positions_and_mapping()
        policy = _policy(
            monthly_investable_cash_eur=Decimal("500"),
            investable_cash_eur=Decimal("500"),
            horizon_months=18,
        )
        plan = new_money_plan(positions, policy, mapping)
        assert plan.months_to_reenter_band is not None
        assert plan.months_to_reenter_band > 18

    def test_flags_but_no_trades_when_gate_closed(self):
        positions, mapping = self._positions_and_mapping()
        policy = _policy(
            monthly_investable_cash_eur=Decimal("500"),
            investable_cash_eur=Decimal("500"),
            horizon_months=18,
            allow_legacy_etf_sales=False,
        )
        plan = new_money_plan(positions, policy, mapping)
        # Advisory flags should mention the constraint
        assert len(plan.legacy_sell_flags) > 0
        assert plan.legacy_sell_trades == []

    def test_actual_trades_when_gate_open(self):
        positions, mapping = self._positions_and_mapping()
        policy = _policy(
            monthly_investable_cash_eur=Decimal("500"),
            investable_cash_eur=Decimal("500"),
            horizon_months=18,
            allow_legacy_etf_sales=True,
        )
        # VTI is a legacy_fund_or_etf and portfolio is outside band for > horizon months
        plan = new_money_plan(positions, policy, mapping)
        assert len(plan.legacy_sell_trades) > 0


# ---------------------------------------------------------------------------
# TestIRAFrozen
# ---------------------------------------------------------------------------

class TestIRAFrozen:
    """IRA/Roth positions must be excluded from buy routing and sell recommendations."""

    def _plan(self):
        positions = [
            _pos("VTI",  Decimal("40000"), "Taxable",          AccountType.TAXABLE),
            _pos("VTSAX", Decimal("30000"), "Roth IRA",         AccountType.ROTH_IRA),
            _pos("VINIX", Decimal("30000"), "Traditional IRA",  AccountType.TRADITIONAL_IRA),
            _pos("BND",  Decimal("20000"), "Taxable",          AccountType.TAXABLE),
        ]
        mapping = {
            "VTI":   _mapping("us_equity"),
            "VTSAX": _mapping("us_equity"),
            "VINIX": _mapping("us_equity"),
            "BND":   _mapping("bonds"),
        }
        policy = _policy(
            buy_enabled_account_types=frozenset({"taxable"}),
            investable_cash_eur=Decimal("5000"),
        )
        return new_money_plan(positions, policy, mapping), positions

    def test_all_buy_trades_in_taxable(self):
        plan, _ = self._plan()
        all_trades = plan.equity_instructions + plan.defensive_instructions
        for t in all_trades:
            assert t.account_type == AccountType.TAXABLE, (
                f"Trade in non-taxable account: {t.account_type} {t.ticker}"
            )

    def test_implementable_view_excludes_ira(self):
        plan, _ = self._plan()
        assert plan.implementable_view.excluded_value > 0

    def test_no_sell_recommendations_targeting_ira(self):
        plan, _ = self._plan()
        for t in plan.legacy_sell_trades:
            assert t.account_type not in {AccountType.ROTH_IRA, AccountType.TRADITIONAL_IRA}


# ---------------------------------------------------------------------------
# TestMonthsToReenterBand
# ---------------------------------------------------------------------------

class TestMonthsToReenterBand:
    """Unit tests for the months_to_reenter_band algebra."""

    def test_overweight_returns_positive_months(self):
        # 87% stocks, target 80% ±5% → overweight
        n = months_to_reenter_band(
            stock_value_usd=Decimal("870000"),
            total_value_usd=Decimal("1000000"),
            target_stock_pct=Decimal("0.80"),
            band_abs=Decimal("0.05"),
            monthly_new_cash_usd=Decimal("550"),  # 500 EUR * 1.10
        )
        assert n is not None
        assert n > 0

    def test_within_band_returns_zero(self):
        # Exactly at target 80%
        n = months_to_reenter_band(
            stock_value_usd=Decimal("800000"),
            total_value_usd=Decimal("1000000"),
            target_stock_pct=Decimal("0.80"),
            band_abs=Decimal("0.05"),
            monthly_new_cash_usd=Decimal("550"),
        )
        assert n == Decimal("0")

    def test_zero_monthly_returns_none(self):
        n = months_to_reenter_band(
            stock_value_usd=Decimal("870000"),
            total_value_usd=Decimal("1000000"),
            target_stock_pct=Decimal("0.80"),
            band_abs=Decimal("0.05"),
            monthly_new_cash_usd=ZERO,
        )
        assert n is None

    def test_underweight_returns_positive_months(self):
        # 73% stocks, target 80% ±5% → underweight
        n = months_to_reenter_band(
            stock_value_usd=Decimal("730000"),
            total_value_usd=Decimal("1000000"),
            target_stock_pct=Decimal("0.80"),
            band_abs=Decimal("0.05"),
            monthly_new_cash_usd=Decimal("550"),
        )
        assert n is not None
        assert n > 0


# ---------------------------------------------------------------------------
# TestPolicyHelpers
# ---------------------------------------------------------------------------

class TestPolicyHelpers:
    """Unit tests for is_etf_buy_blocked and is_account_buy_enabled."""

    def test_legacy_fund_blocked(self):
        assert is_etf_buy_blocked(InstrumentType.legacy_fund_or_etf)

    def test_us_etf_blocked(self):
        assert is_etf_buy_blocked(InstrumentType.us_etf)

    def test_ucits_etf_blocked(self):
        assert is_etf_buy_blocked(InstrumentType.ucits_etf)

    def test_us_equity_not_blocked(self):
        assert not is_etf_buy_blocked(InstrumentType.us_equity)

    def test_cash_not_in_blocked_buy_types(self):
        # cash is non-buyable by omission, not by explicit block
        assert InstrumentType.cash not in BLOCKED_BUY_TYPES

    def test_taxable_buy_enabled(self):
        policy = _policy()
        assert is_account_buy_enabled(AccountType.TAXABLE, policy)

    def test_roth_not_buy_enabled_by_default(self):
        policy = _policy()
        assert not is_account_buy_enabled(AccountType.ROTH_IRA, policy)

    def test_roth_buy_enabled_when_configured(self):
        policy = _policy(buy_enabled_account_types=frozenset({"taxable", "roth_ira"}))
        assert is_account_buy_enabled(AccountType.ROTH_IRA, policy)


# ---------------------------------------------------------------------------
# TestLegacySellFlags
# ---------------------------------------------------------------------------

class TestLegacySellFlags:
    """Unit tests for build_legacy_sell_flags."""

    def _etf_position(self, never_want=False):
        pos = _pos("VTI", Decimal("10000"))
        mapping = {
            "VTI": TickerMapping(
                asset_class="us_equity",
                instrument_type=InstrumentType.legacy_fund_or_etf,
                never_want=never_want,
            )
        }
        return [pos], mapping

    def test_never_want_always_flagged_advisory(self):
        positions, mapping = self._etf_position(never_want=True)
        policy = _policy(allow_legacy_etf_sales=False)
        flags, trades = build_legacy_sell_flags(positions, mapping, policy, True, Decimal("10"))
        assert len(flags) > 0
        assert trades == []

    def test_never_want_generates_trade_when_gate_open(self):
        positions, mapping = self._etf_position(never_want=True)
        policy = _policy(allow_legacy_etf_sales=True)
        flags, trades = build_legacy_sell_flags(positions, mapping, policy, True, Decimal("10"))
        assert len(trades) == 1
        assert trades[0].ticker == "VTI"

    def test_within_band_no_flag(self):
        positions, mapping = self._etf_position(never_want=False)
        policy = _policy(allow_legacy_etf_sales=True)
        flags, trades = build_legacy_sell_flags(
            positions, mapping, policy,
            outside_band=False,
            months_to_fix=Decimal("5"),
        )
        assert trades == []


# ---------------------------------------------------------------------------
# TestComputeAllocationViews
# ---------------------------------------------------------------------------

class TestComputeAllocationViews:
    """Unit tests for compute_allocation_views — band detection uses TOTAL view."""

    def test_total_view_detects_overweight(self):
        positions = [
            _pos("VTI", Decimal("87000")),
            _pos("BND", Decimal("13000")),
            _pos("VTSAX", Decimal("30000"), "Roth IRA", AccountType.ROTH_IRA),
        ]
        mapping = {
            "VTI":   _mapping("us_equity"),
            "BND":   _mapping("bonds"),
            "VTSAX": _mapping("us_equity"),
        }
        policy = _policy(buy_enabled_account_types=frozenset({"taxable"}))
        total_view, impl_view = compute_allocation_views(positions, policy, mapping)
        # Total view sees all accounts → should detect overweight
        assert not total_view.within_bands
        # Implementable view only has taxable accounts
        assert impl_view.excluded_value > 0

    def test_implementable_view_excludes_ira(self):
        positions = [
            _pos("VTI",  Decimal("80000"), "Taxable",   AccountType.TAXABLE),
            _pos("VTSAX", Decimal("20000"), "Roth IRA", AccountType.ROTH_IRA),
        ]
        mapping = {
            "VTI":   _mapping("us_equity"),
            "VTSAX": _mapping("us_equity"),
        }
        policy = _policy(buy_enabled_account_types=frozenset({"taxable"}))
        _, impl_view = compute_allocation_views(positions, policy, mapping)
        assert impl_view.excluded_value == Decimal("20000")

    def test_asset_class_drives_classification_not_instrument_type(self):
        """instrument_type=us_equity but asset_class=bonds → classified as defensive."""
        positions = [
            _pos("XYZ", Decimal("50000")),
            _pos("ABC", Decimal("50000")),
        ]
        mapping = {
            "XYZ": TickerMapping(asset_class="us_equity", instrument_type=InstrumentType.legacy_fund_or_etf),
            "ABC": TickerMapping(asset_class="bonds", instrument_type=InstrumentType.us_equity),
        }
        policy = _policy()
        total_view, _ = compute_allocation_views(positions, policy, mapping)
        assert total_view.stock_pct == Decimal("0.5000")
        assert total_view.bond_pct == Decimal("0.5000")
