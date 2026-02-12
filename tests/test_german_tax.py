from decimal import Decimal

import pytest

from rebalancer.german_tax import (
    annotate_trades,
    check_pfic_risk,
    classify_fund,
    generate_summary,
    get_teilfreistellung,
)
from rebalancer.models import (
    AccountType,
    GermanFundCategory,
    GermanTaxConfig,
    TickerMapping,
    Trade,
)


# ---------------------------------------------------------------------------
# classify_fund
# ---------------------------------------------------------------------------

class TestClassifyFund:
    def test_classify_us_equity(self):
        assert classify_fund("us_equity") == GermanFundCategory.AKTIENFONDS

    def test_classify_intl_equity(self):
        assert classify_fund("intl_equity") == GermanFundCategory.AKTIENFONDS

    def test_classify_reit_as_aktienfonds(self):
        """REIT ETFs hold equities, not direct real estate -> Aktienfonds."""
        assert classify_fund("reit") == GermanFundCategory.AKTIENFONDS

    def test_classify_bonds(self):
        assert classify_fund("bonds") == GermanFundCategory.OTHER

    def test_classify_cash(self):
        assert classify_fund("cash") == GermanFundCategory.OTHER

    def test_classify_unknown_defaults_to_other(self):
        assert classify_fund("crypto") == GermanFundCategory.OTHER


# ---------------------------------------------------------------------------
# get_teilfreistellung
# ---------------------------------------------------------------------------

class TestTeilfreistellung:
    def test_aktienfonds_rate(self):
        assert get_teilfreistellung(GermanFundCategory.AKTIENFONDS) == Decimal("30")

    def test_mischfonds_rate(self):
        assert get_teilfreistellung(GermanFundCategory.MISCHFONDS) == Decimal("15")

    def test_immobilienfonds_rate(self):
        assert get_teilfreistellung(GermanFundCategory.IMMOBILIENFONDS) == Decimal("60")

    def test_other_rate(self):
        assert get_teilfreistellung(GermanFundCategory.OTHER) == Decimal("0")


# ---------------------------------------------------------------------------
# check_pfic_risk
# ---------------------------------------------------------------------------

class TestPficRisk:
    def test_us_domicile_no_risk(self):
        assert check_pfic_risk("US") is False

    def test_us_lowercase_no_risk(self):
        assert check_pfic_risk("us") is False

    def test_ireland_domicile_has_risk(self):
        assert check_pfic_risk("IE") is True

    def test_luxembourg_domicile_has_risk(self):
        assert check_pfic_risk("LU") is True


# ---------------------------------------------------------------------------
# annotate_trades
# ---------------------------------------------------------------------------

def _make_trade(ticker: str, account_type: AccountType, action: str = "BUY") -> Trade:
    return Trade(
        account_name="Test Account",
        account_type=account_type,
        ticker=ticker,
        action=action,
        shares=Decimal("10"),
        estimated_value=Decimal("1000"),
        reasoning="test",
    )


def _make_mapping(**kwargs) -> dict[str, TickerMapping]:
    result = {}
    for ticker, info in kwargs.items():
        if isinstance(info, tuple):
            result[ticker] = TickerMapping(
                asset_class=info[0], domicile=info[1]
            )
        else:
            result[ticker] = TickerMapping(asset_class=info)
    return result


class TestAnnotateTrades:
    def test_taxable_trades_get_annotations(self):
        trades = [_make_trade("VTI", AccountType.TAXABLE)]
        mapping = _make_mapping(VTI="us_equity")
        config = GermanTaxConfig(enabled=True)

        annotations = annotate_trades(trades, mapping, config)
        assert len(annotations) == 1
        assert annotations[0].ticker == "VTI"
        assert annotations[0].fund_category == GermanFundCategory.AKTIENFONDS

    def test_skips_tax_advantaged_accounts(self):
        trades = [
            _make_trade("VTI", AccountType.ROTH_IRA),
            _make_trade("BND", AccountType.TRADITIONAL_IRA),
            _make_trade("FXAIX", AccountType.FOUR_01K),
        ]
        mapping = _make_mapping(VTI="us_equity", BND="bonds", FXAIX="us_equity")
        config = GermanTaxConfig(enabled=True)

        annotations = annotate_trades(trades, mapping, config)
        assert len(annotations) == 0

    def test_mixed_accounts_only_annotates_taxable(self):
        trades = [
            _make_trade("VTI", AccountType.TAXABLE),
            _make_trade("VTI", AccountType.ROTH_IRA),
            _make_trade("BND", AccountType.TRADITIONAL_IRA),
        ]
        mapping = _make_mapping(VTI="us_equity", BND="bonds")
        config = GermanTaxConfig(enabled=True)

        annotations = annotate_trades(trades, mapping, config)
        assert len(annotations) == 1
        assert annotations[0].ticker == "VTI"

    def test_pfic_risk_flagged_for_non_us(self):
        trades = [_make_trade("IWDA", AccountType.TAXABLE)]
        mapping = _make_mapping(IWDA=("intl_equity", "IE"))
        config = GermanTaxConfig(enabled=True)

        annotations = annotate_trades(trades, mapping, config)
        assert len(annotations) == 1
        assert annotations[0].pfic_risk is True
        assert annotations[0].domicile == "IE"

    def test_no_pfic_risk_for_us_domicile(self):
        trades = [_make_trade("VTI", AccountType.TAXABLE)]
        mapping = _make_mapping(VTI="us_equity")
        config = GermanTaxConfig(enabled=True)

        annotations = annotate_trades(trades, mapping, config)
        assert annotations[0].pfic_risk is False

    def test_no_annotations_when_disabled(self):
        trades = [_make_trade("VTI", AccountType.TAXABLE)]
        mapping = _make_mapping(VTI="us_equity")
        config = GermanTaxConfig(enabled=False)

        annotations = annotate_trades(trades, mapping, config)
        assert len(annotations) == 0

    def test_skips_ticker_not_in_mapping(self):
        trades = [_make_trade("UNKNOWN", AccountType.TAXABLE)]
        mapping = _make_mapping(VTI="us_equity")
        config = GermanTaxConfig(enabled=True)

        annotations = annotate_trades(trades, mapping, config)
        assert len(annotations) == 0

    def test_annotation_notes_contain_teilfreistellung(self):
        trades = [_make_trade("VTI", AccountType.TAXABLE)]
        mapping = _make_mapping(VTI="us_equity")
        config = GermanTaxConfig(enabled=True)

        annotations = annotate_trades(trades, mapping, config)
        notes = annotations[0].notes
        assert any("Teilfreistellung" in n for n in notes)
        assert any("30%" in n for n in notes)

    def test_bonds_get_zero_teilfreistellung(self):
        trades = [_make_trade("BND", AccountType.TAXABLE)]
        mapping = _make_mapping(BND="bonds")
        config = GermanTaxConfig(enabled=True)

        annotations = annotate_trades(trades, mapping, config)
        assert annotations[0].teilfreistellung_pct == Decimal("0")
        assert any("26.375%" in n for n in annotations[0].notes)

    def test_deduplicates_tickers(self):
        trades = [
            _make_trade("VTI", AccountType.TAXABLE),
            _make_trade("VTI", AccountType.TAXABLE),
        ]
        mapping = _make_mapping(VTI="us_equity")
        config = GermanTaxConfig(enabled=True)

        annotations = annotate_trades(trades, mapping, config)
        assert len(annotations) == 1

    def test_german_fund_category_override(self):
        """Explicit german_fund_category in mapping overrides heuristic."""
        trades = [_make_trade("BND", AccountType.TAXABLE)]
        mapping = {
            "BND": TickerMapping(
                asset_class="bonds",
                german_fund_category="mischfonds",
            )
        }
        config = GermanTaxConfig(enabled=True)
        annotations = annotate_trades(trades, mapping, config)
        assert annotations[0].fund_category == GermanFundCategory.MISCHFONDS
        assert annotations[0].teilfreistellung_pct == Decimal("15")
        assert any("from mapping" in n for n in annotations[0].notes)

    def test_german_fund_category_fallback(self):
        """Without override, heuristic is used and noted."""
        trades = [_make_trade("VTI", AccountType.TAXABLE)]
        mapping = _make_mapping(VTI="us_equity")
        config = GermanTaxConfig(enabled=True)
        annotations = annotate_trades(trades, mapping, config)
        assert annotations[0].fund_category == GermanFundCategory.AKTIENFONDS
        assert any("inferred from asset class" in n for n in annotations[0].notes)

    def test_is_accumulating_true(self):
        """is_accumulating=True adds Vorabpauschale note."""
        trades = [_make_trade("IWDA", AccountType.TAXABLE)]
        mapping = {
            "IWDA": TickerMapping(
                asset_class="intl_equity",
                domicile="IE",
                is_accumulating=True,
            )
        }
        config = GermanTaxConfig(enabled=True)
        annotations = annotate_trades(trades, mapping, config)
        assert annotations[0].is_accumulating is True
        assert any("Vorabpauschale" in n for n in annotations[0].notes)

    def test_is_accumulating_unknown(self):
        """is_accumulating=None adds 'unknown -- verify' note."""
        trades = [_make_trade("VTI", AccountType.TAXABLE)]
        mapping = _make_mapping(VTI="us_equity")
        config = GermanTaxConfig(enabled=True)
        annotations = annotate_trades(trades, mapping, config)
        assert any("unknown" in n for n in annotations[0].notes)

    def test_is_accumulating_false(self):
        """is_accumulating=False produces no Vorabpauschale note."""
        trades = [_make_trade("VTI", AccountType.TAXABLE)]
        mapping = {
            "VTI": TickerMapping(
                asset_class="us_equity",
                is_accumulating=False,
            )
        }
        config = GermanTaxConfig(enabled=True)
        annotations = annotate_trades(trades, mapping, config)
        assert not any("Vorabpauschale" in n for n in annotations[0].notes)
        assert not any("unknown" in n for n in annotations[0].notes)

    def test_invalid_german_fund_category(self):
        """Invalid german_fund_category falls back to OTHER."""
        trades = [_make_trade("XYZ", AccountType.TAXABLE)]
        mapping = {
            "XYZ": TickerMapping(
                asset_class="us_equity",
                german_fund_category="invalid_category",
            )
        }
        config = GermanTaxConfig(enabled=True)
        annotations = annotate_trades(trades, mapping, config)
        assert annotations[0].fund_category == GermanFundCategory.OTHER
        assert any("UNKNOWN" in n for n in annotations[0].notes)


# ---------------------------------------------------------------------------
# generate_summary
# ---------------------------------------------------------------------------

class TestGenerateSummary:
    def test_sparerpauschbetrag_single(self):
        from rebalancer.models import GermanTaxAnnotation, GermanFundCategory

        annotations = [
            GermanTaxAnnotation(
                ticker="VTI",
                fund_category=GermanFundCategory.AKTIENFONDS,
                teilfreistellung_pct=Decimal("30"),
            )
        ]
        summary = generate_summary(annotations, "single")
        assert summary["sparerpauschbetrag_eur"] == 1000

    def test_sparerpauschbetrag_married(self):
        from rebalancer.models import GermanTaxAnnotation, GermanFundCategory

        annotations = [
            GermanTaxAnnotation(
                ticker="VTI",
                fund_category=GermanFundCategory.AKTIENFONDS,
                teilfreistellung_pct=Decimal("30"),
            )
        ]
        summary = generate_summary(annotations, "married")
        assert summary["sparerpauschbetrag_eur"] == 2000

    def test_pfic_count(self):
        from rebalancer.models import GermanTaxAnnotation, GermanFundCategory

        annotations = [
            GermanTaxAnnotation(
                ticker="VTI",
                fund_category=GermanFundCategory.AKTIENFONDS,
                teilfreistellung_pct=Decimal("30"),
                pfic_risk=False,
            ),
            GermanTaxAnnotation(
                ticker="IWDA",
                fund_category=GermanFundCategory.AKTIENFONDS,
                teilfreistellung_pct=Decimal("30"),
                pfic_risk=True,
                domicile="IE",
            ),
        ]
        summary = generate_summary(annotations, "single")
        assert summary["pfic_risk_count"] == 1
        assert summary["total_annotated"] == 2
