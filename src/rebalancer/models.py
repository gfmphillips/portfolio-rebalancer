from decimal import Decimal
from enum import Enum
from typing import Literal

from pydantic import BaseModel


class AccountType(str, Enum):
    TAXABLE = "taxable"
    TRADITIONAL_IRA = "traditional_ira"
    ROTH_IRA = "roth_ira"
    ROTH_401K = "roth_401k"
    FOUR_01K = "401k"
    HSA = "hsa"


class Position(BaseModel):
    account_name: str
    account_type: AccountType
    ticker: str
    description: str
    quantity: Decimal
    price: Decimal
    market_value: Decimal
    cost_basis_total: Decimal | None = None


class TickerMapping(BaseModel):
    asset_class: str
    similar_tickers: list[str] = []


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


class TaxImpact(BaseModel):
    estimated_total_gains: Decimal = Decimal("0")
    estimated_total_losses: Decimal = Decimal("0")
    estimated_net: Decimal = Decimal("0")
    taxable_trades_count: int = 0


class RebalanceResult(BaseModel):
    total_portfolio_value: Decimal
    current_allocation: dict[str, Decimal]
    target_allocation: dict[str, Decimal]
    drift: dict[str, Decimal]
    trades: list[Trade]
    warnings: list[str]
    tax_impact: TaxImpact = TaxImpact()


class RebalanceConfig(BaseModel):
    threshold_pct: Decimal = Decimal("3.0")
    min_trade_value: Decimal = Decimal("50")
    tlh_enabled: bool = True
    avoid_gains_in_taxable: bool = True
    cash_to_invest: Decimal = Decimal("0")
    account_mappings: dict[str, AccountType] = {}
