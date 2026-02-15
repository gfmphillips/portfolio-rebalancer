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
    german_fund_category: str | None = None  # "aktienfonds", "mischfonds", etc.
    is_accumulating: bool | None = None  # None = unknown


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
