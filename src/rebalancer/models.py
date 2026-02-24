from dataclasses import dataclass, field as dc_field
from decimal import Decimal
from enum import Enum
from typing import Literal, NamedTuple

from pydantic import BaseModel

ZERO = Decimal("0")
HUNDRED = Decimal("100")


class AccountType(str, Enum):
    TAXABLE = "taxable"
    TRADITIONAL_IRA = "traditional_ira"
    ROTH_IRA = "roth_ira"
    ROTH_401K = "roth_401k"
    FOUR_01K = "401k"
    HSA = "hsa"


class InstrumentType(str, Enum):
    """Buy/sell classification for a ticker.

    Default is ``legacy_fund_or_etf`` (conservative: hold/sell only).
    UCITS vs US ETF distinction is NOT auto-inferred; set via user overrides only.
    """
    legacy_fund_or_etf = "legacy_fund_or_etf"  # any fund/ETF; hold/sell only (DEFAULT)
    us_equity          = "us_equity"            # individual stock; buyable
    us_bond            = "us_bond"              # individual bond; buyable
    us_treasury        = "us_treasury"          # Treasury; buyable
    cd                 = "cd"                   # CD; buyable
    cash               = "cash"                 # money-market; hold only
    us_etf             = "us_etf"               # user override: US-domiciled ETF; sell-only
    ucits_etf          = "ucits_etf"            # user override: UCITS/PFIC ETF; sell-only


BUYABLE_TYPES: frozenset[InstrumentType] = frozenset({
    InstrumentType.us_equity,
    InstrumentType.us_bond,
    InstrumentType.us_treasury,
    InstrumentType.cd,
})

SELL_ONLY_TYPES: frozenset[InstrumentType] = frozenset({
    InstrumentType.legacy_fund_or_etf,
    InstrumentType.us_etf,
    InstrumentType.ucits_etf,
})

# Cash is excluded from BLOCKED_BUY_TYPES; it is non-buyable by omission (not "blocked").
BLOCKED_BUY_TYPES: frozenset[InstrumentType] = frozenset({
    InstrumentType.legacy_fund_or_etf,
    InstrumentType.us_etf,
    InstrumentType.ucits_etf,
})

# Asset-class strings that count as "stock" or "defensive" in allocation math.
# These are the source of truth; instrument_type controls buy-eligibility only.
STOCK_ASSET_CLASSES: frozenset[str] = frozenset({"us_equity", "intl_equity", "reit"})
DEFENSIVE_ASSET_CLASSES: frozenset[str] = frozenset({"bonds", "cash"})


class DefensiveMode(str, Enum):
    """How to generate defensive placeholder instructions."""
    treasury_only     = "treasury_only"      # DEFAULT: single TREASURY row
    treasury_cd_split = "treasury_cd_split"  # TREASURY + CD rows per treasury_pct/cd_pct
    ladder            = "ladder"             # one row per rung in ladder_rungs_months


TAX_ADVANTAGED = {
    AccountType.TRADITIONAL_IRA,
    AccountType.ROTH_IRA,
    AccountType.ROTH_401K,
    AccountType.FOUR_01K,
    AccountType.HSA,
}


class TaxLot(BaseModel):
    acquisition_date: str  # YYYY-MM-DD
    shares: Decimal
    cost_basis_per_share: Decimal


class Position(BaseModel):
    account_name: str
    account_type: AccountType
    ticker: str
    description: str
    quantity: Decimal
    price: Decimal
    market_value: Decimal
    cost_basis_total: Decimal | None = None
    tax_lots: list[TaxLot] = []


class TickerMapping(BaseModel):
    asset_class: str
    similar_tickers: list[str] = []
    domicile: str = "US"
    preferred: bool = False
    consolidate_to: str | None = None
    price: Decimal | None = None  # fallback price for preferred tickers not yet held
    german_fund_category: str | None = None  # "aktienfonds", "mischfonds", etc.
    is_accumulating: bool | None = None  # None = unknown
    # Policy-aware fields (new):
    instrument_type: InstrumentType = InstrumentType.legacy_fund_or_etf
    never_want: bool = False  # user flags a holding they want to eventually exit


class AllocationTarget(BaseModel):
    asset_class: str
    target_pct: Decimal


class Trade(BaseModel):
    account_name: str
    account_type: AccountType
    ticker: str
    action: Literal["BUY", "SELL"]
    shares: Decimal
    estimated_value: Decimal
    reasoning: str
    warnings: list[str] = []
    estimated_gain_loss: Decimal | None = None  # positive = gain, negative = loss
    lot_acquisition_date: str | None = None


class TaxImpact(BaseModel):
    estimated_total_gains: Decimal = ZERO
    estimated_total_losses: Decimal = ZERO
    estimated_net: Decimal = ZERO
    taxable_trades_count: int = 0


class ConstraintCheck(BaseModel):
    name: str
    required: Decimal
    actual: Decimal
    met: bool
    message: str


class ConstraintsConfig(BaseModel):
    min_taxable_bonds_usd: Decimal | None = None


class RunMetadata(BaseModel):
    timestamp: str  # ISO 8601
    eurusd_fx_used: Decimal
    tool_version: str


class RebalanceResult(BaseModel):
    total_portfolio_value: Decimal
    current_allocation: dict[str, Decimal]
    target_allocation: dict[str, Decimal]
    drift: dict[str, Decimal]
    trades: list[Trade]
    warnings: list[str]
    tax_impact: TaxImpact = TaxImpact()
    constraints: list[ConstraintCheck] = []
    metadata: RunMetadata | None = None


class PrecisionConfig(BaseModel):
    currency: int = 0  # 0→"$1,234", 2→"$1,234.56"
    pct: int = 2


class SortKey(str, Enum):
    SELLS_FIRST = "sells_first"
    BUYS_FIRST = "buys_first"
    LARGEST_TRADE_FIRST = "largest_trade_first"
    SMALLEST_TRADE_FIRST = "smallest_trade_first"
    BY_ACCOUNT = "by_account"
    BY_TICKER = "by_ticker"


class OutputConfig(BaseModel):
    show_only_actionable_trades: bool = True
    sort_order: list[SortKey] = [SortKey.SELLS_FIRST, SortKey.LARGEST_TRADE_FIRST]
    precision: PrecisionConfig = PrecisionConfig()


class CashCategory(BaseModel):
    eur: Decimal = ZERO
    usd: Decimal = ZERO


class CashConfig(BaseModel):
    eurusd_fx: Decimal = Decimal("1.10")
    investable: CashCategory = CashCategory()
    emergency: CashCategory = CashCategory()
    # Legacy fields (deprecated, mapped to investable/emergency on load)
    include_in_portfolio: bool = True
    external_cash_eur: Decimal = ZERO
    external_cash_usd: Decimal = ZERO


class GermanFundCategory(str, Enum):
    AKTIENFONDS = "aktienfonds"
    MISCHFONDS = "mischfonds"
    IMMOBILIENFONDS = "immobilienfonds"
    OTHER = "other"


class GermanTaxAnnotation(BaseModel):
    ticker: str
    fund_category: GermanFundCategory
    teilfreistellung_pct: Decimal
    is_accumulating: bool = False
    pfic_risk: bool = False
    domicile: str = "US"
    notes: list[str] = []


class GermanTaxConfig(BaseModel):
    enabled: bool = False
    filing_status: str = "single"
    kirchensteuer: bool = False


class Transaction(BaseModel):
    date: str  # YYYY-MM-DD
    account_name: str
    ticker: str
    action: Literal["BUY", "SELL"]
    shares: Decimal


class RebalanceConfig(BaseModel):
    threshold_pct: Decimal = Decimal("3.0")
    threshold_relative_pct: Decimal = Decimal("20")
    min_trade_value: Decimal = Decimal("50")
    tlh_enabled: bool = True
    avoid_gains_in_taxable: bool = True
    cash_to_invest: Decimal = ZERO
    account_mappings: dict[str, AccountType] = {}
    whole_shares_only: bool = False  # when True, round all share quantities to whole numbers


class ConsolidationOpportunity(BaseModel):
    ticker: str
    account_name: str
    account_type: AccountType
    market_value: Decimal
    consolidate_to: str
    safe_to_consolidate: bool
    estimated_gain_loss: Decimal | None = None
    reason: str


class ConsolidationAnalysis(BaseModel):
    end_state_value: Decimal
    legacy_value: Decimal
    end_state_pct: Decimal
    legacy_pct: Decimal
    opportunities: list[ConsolidationOpportunity]


class UnifiedConfig(NamedTuple):
    targets: list[AllocationTarget]
    rebalance_config: RebalanceConfig
    output_config: OutputConfig
    cash_config: CashConfig
    german_tax_config: GermanTaxConfig
    constraints_config: ConstraintsConfig


# ---------------------------------------------------------------------------
# Policy-aware engine data model
# ---------------------------------------------------------------------------

@dataclass
class PolicyConfig:
    """All settings for the new-money-only policy engine."""

    # Allocation targets (as fractions, e.g. 0.80 = 80%)
    target_stock_pct: Decimal                = Decimal("0.80")
    target_bond_pct: Decimal                 = Decimal("0.20")
    target_us_equity_pct_of_equity: Decimal  = Decimal("0.60")

    # Cash inputs
    bank_cash_target_eur: Decimal            = Decimal("100000")
    investable_cash_eur: Decimal             = Decimal("0")
    monthly_investable_cash_eur: Decimal     = Decimal("0")
    eurusd_fx: Decimal                       = Decimal("1.10")

    # Rebalance behaviour
    rebalance_band_abs: Decimal              = Decimal("0.05")   # 5 pp absolute band
    horizon_months: int                      = 18
    basket_size: int                         = 75
    min_trade_value: Decimal                 = Decimal("50")

    # Feature flags
    allow_international_basket: bool         = False
    allow_legacy_etf_sales: bool             = False

    # Account routing — set of AccountType.value strings that are buy-enabled
    buy_enabled_account_types: frozenset     = dc_field(
        default_factory=lambda: frozenset({"taxable"})
    )

    # Defensive allocation mode
    defensive_mode: DefensiveMode            = DefensiveMode.treasury_only
    treasury_pct: Decimal                    = Decimal("1.00")   # fraction; used for treasury_cd_split
    cd_pct: Decimal                          = Decimal("0.00")   # fraction; used for treasury_cd_split
    ladder_rungs_months: list[int]           = dc_field(
        default_factory=lambda: [6, 12, 24, 36]
    )
    ladder_currency: str                     = "EUR"

    # Basket metadata (informational; not used by engine logic)
    basket_csv_path: str | None              = None
    basket_version: str | None               = None   # "YYYY-MM-DD" extracted from filename


@dataclass
class BasketConstituent:
    """One constituent of a stock basket CSV."""
    ticker: str
    target_weight: Decimal   # normalized to sum=1 before use
    name: str    = ""
    sector: str  = ""
    country: str = ""
    is_adr: bool = False


@dataclass
class AllocationView:
    """Stock/defensive breakdown from one perspective (total or implementable)."""
    label: str                  # "Total Portfolio" | "Implementable"
    total_value: Decimal
    stock_value: Decimal
    defensive_value: Decimal
    stock_pct: Decimal          # current stock fraction
    bond_pct: Decimal           # current defensive fraction
    target_stock_pct: Decimal
    target_bond_pct: Decimal
    stock_drift: Decimal        # stock_pct - target_stock_pct (positive = overweight)
    within_bands: bool
    excluded_value: Decimal     # value not counted in this view
    excluded_reason: str        # e.g. "IRA/Roth accounts (buy-blocked)"


@dataclass
class BuyPlan:
    """Output of new_money_plan() — everything needed to render the Buy Plan tab."""
    total_view: AllocationView
    implementable_view: AllocationView
    investable_cash_usd: Decimal
    equity_cash_usd: Decimal           # portion directed → equity buys
    defensive_cash_usd: Decimal        # portion directed → defensive placeholders
    equity_instructions: list[Trade]   # individual stock BUYs (or basket placeholder)
    defensive_instructions: list[Trade]  # TREASURY/CD/ladder placeholder rows
    legacy_sell_flags: list[str]       # advisory warnings (always built)
    legacy_sell_trades: list[Trade]    # non-empty only if allow_legacy_etf_sales=True
    why_text: str                      # human-readable rationale
    warnings: list[str]
    months_to_reenter_band: Decimal | None = None  # None if monthly_cash == 0
