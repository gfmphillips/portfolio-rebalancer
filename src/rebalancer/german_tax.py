"""German tax advisory layer.

Generates advisory annotations for taxable-account trades based on German
investment tax rules (InvStG). This module is informational only and does
not modify the rebalance engine.
"""

from decimal import Decimal

from .models import (
    AccountType,
    GermanFundCategory,
    GermanTaxAnnotation,
    GermanTaxConfig,
    TickerMapping,
    Trade,
)

# Tax-advantaged account types (not subject to German tax per DBA Art. 18A)
_TAX_ADVANTAGED = {
    AccountType.TRADITIONAL_IRA,
    AccountType.ROTH_IRA,
    AccountType.ROTH_401K,
    AccountType.FOUR_01K,
    AccountType.HSA,
}

_TEILFREISTELLUNG: dict[GermanFundCategory, Decimal] = {
    GermanFundCategory.AKTIENFONDS: Decimal("30"),
    GermanFundCategory.MISCHFONDS: Decimal("15"),
    GermanFundCategory.IMMOBILIENFONDS: Decimal("60"),
    GermanFundCategory.OTHER: Decimal("0"),
}

_ABGELTUNGSTEUER = Decimal("26.375")  # 25% + 5.5% Soli


def classify_fund(asset_class: str) -> GermanFundCategory:
    """Classify a fund into a German InvStG category based on its asset class.

    REIT ETFs (VNQ, VNQI) are classified as Aktienfonds because they hold
    REIT company equities, not direct real estate (SS 2 InvStG).
    """
    if asset_class in ("us_equity", "intl_equity", "reit"):
        return GermanFundCategory.AKTIENFONDS
    return GermanFundCategory.OTHER


def get_teilfreistellung(category: GermanFundCategory) -> Decimal:
    """Return the Teilfreistellung percentage for a fund category."""
    return _TEILFREISTELLUNG[category]


def check_pfic_risk(domicile: str) -> bool:
    """Return True if the fund is domiciled outside the US (PFIC risk)."""
    return domicile.upper() != "US"


def _effective_tax_rate(teilfreistellung_pct: Decimal) -> Decimal:
    """Compute effective German tax rate after Teilfreistellung."""
    taxable_fraction = Decimal("100") - teilfreistellung_pct
    return (_ABGELTUNGSTEUER * taxable_fraction / Decimal("100")).quantize(
        Decimal("0.01")
    )


def annotate_trades(
    trades: list[Trade],
    mapping: dict[str, TickerMapping],
    config: GermanTaxConfig,
) -> list[GermanTaxAnnotation]:
    """Generate German tax annotations for taxable-account trades.

    Skips tax-advantaged accounts (IRA, Roth, 401k, HSA) per DBA Art. 18A.
    Returns an empty list when german_tax is disabled.
    """
    if not config.enabled:
        return []

    # Collect unique tickers from taxable-account trades
    seen: set[str] = set()
    taxable_tickers: list[str] = []
    for t in trades:
        if t.account_type in _TAX_ADVANTAGED:
            continue
        if t.ticker not in seen:
            seen.add(t.ticker)
            taxable_tickers.append(t.ticker)

    annotations: list[GermanTaxAnnotation] = []
    for ticker in taxable_tickers:
        tm = mapping.get(ticker)
        if tm is None:
            continue

        category = classify_fund(tm.asset_class)
        tf_pct = get_teilfreistellung(category)
        pfic = check_pfic_risk(tm.domicile)
        effective = _effective_tax_rate(tf_pct)

        notes: list[str] = []
        if tf_pct > 0:
            notes.append(
                f"{tf_pct}% Teilfreistellung applies -- "
                f"effective tax rate ~{effective}%"
            )
        else:
            notes.append(f"No Teilfreistellung -- full {_ABGELTUNGSTEUER}% rate")

        if pfic:
            notes.append(
                f"PFIC risk: {ticker} is domiciled in {tm.domicile}. "
                "Non-US funds may trigger punitive IRS PFIC taxation."
            )

        annotations.append(
            GermanTaxAnnotation(
                ticker=ticker,
                fund_category=category,
                teilfreistellung_pct=tf_pct,
                pfic_risk=pfic,
                domicile=tm.domicile,
                notes=notes,
            )
        )

    return annotations


def generate_summary(
    annotations: list[GermanTaxAnnotation],
    filing_status: str,
) -> dict:
    """Generate a summary dict of German tax advisory information."""
    sparerpauschbetrag = 2000 if filing_status == "married" else 1000

    pfic_count = sum(1 for a in annotations if a.pfic_risk)
    categories = sorted({a.fund_category.value for a in annotations})

    return {
        "sparerpauschbetrag_eur": sparerpauschbetrag,
        "pfic_risk_count": pfic_count,
        "categories_in_play": categories,
        "total_annotated": len(annotations),
    }
