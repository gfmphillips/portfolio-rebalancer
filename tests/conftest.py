from decimal import Decimal
from pathlib import Path

import pytest

from rebalancer.models import AccountType, Position, RebalanceConfig, TickerMapping


EXAMPLES_DIR = Path(__file__).parent.parent / "examples"


@pytest.fixture
def examples_dir():
    return EXAMPLES_DIR


@pytest.fixture
def sample_positions():
    return [
        Position(
            account_name="Individual - TOD...XXX123456",
            account_type=AccountType.TAXABLE,
            ticker="VTI",
            description="VANGUARD TOTAL STOCK MKT ETF",
            quantity=Decimal("100"),
            price=Decimal("250.00"),
            market_value=Decimal("25000.00"),
            cost_basis_total=Decimal("20000.00"),
        ),
        Position(
            account_name="Individual - TOD...XXX123456",
            account_type=AccountType.TAXABLE,
            ticker="VXUS",
            description="VANGUARD TOTAL INTL STOCK ETF",
            quantity=Decimal("200"),
            price=Decimal("60.00"),
            market_value=Decimal("12000.00"),
            cost_basis_total=Decimal("10000.00"),
        ),
        Position(
            account_name="Individual - TOD...XXX123456",
            account_type=AccountType.TAXABLE,
            ticker="BND",
            description="VANGUARD TOTAL BOND MKT ETF",
            quantity=Decimal("50"),
            price=Decimal("72.00"),
            market_value=Decimal("3600.00"),
            cost_basis_total=Decimal("4000.00"),
        ),
        Position(
            account_name="Individual - TOD...XXX123456",
            account_type=AccountType.TAXABLE,
            ticker="SPAXX",
            description="FIDELITY GOVERNMENT MONEY MARKET",
            quantity=Decimal("0"),
            price=Decimal("0"),
            market_value=Decimal("2400.00"),
            cost_basis_total=None,
        ),
        Position(
            account_name="ROTH IRA...XXX789012",
            account_type=AccountType.ROTH_IRA,
            ticker="FXAIX",
            description="FIDELITY 500 INDEX FUND",
            quantity=Decimal("50"),
            price=Decimal("200.00"),
            market_value=Decimal("10000.00"),
            cost_basis_total=Decimal("7000.00"),
        ),
        Position(
            account_name="ROTH IRA...XXX789012",
            account_type=AccountType.ROTH_IRA,
            ticker="VXUS",
            description="VANGUARD TOTAL INTL STOCK ETF",
            quantity=Decimal("50"),
            price=Decimal("60.00"),
            market_value=Decimal("3000.00"),
            cost_basis_total=Decimal("2500.00"),
        ),
        Position(
            account_name="ROTH IRA...XXX789012",
            account_type=AccountType.ROTH_IRA,
            ticker="BND",
            description="VANGUARD TOTAL BOND MKT ETF",
            quantity=Decimal("50"),
            price=Decimal("72.00"),
            market_value=Decimal("3600.00"),
            cost_basis_total=Decimal("3500.00"),
        ),
        Position(
            account_name="ROTH IRA...XXX789012",
            account_type=AccountType.ROTH_IRA,
            ticker="SPAXX",
            description="FIDELITY GOVERNMENT MONEY MARKET",
            quantity=Decimal("0"),
            price=Decimal("0"),
            market_value=Decimal("1400.00"),
            cost_basis_total=None,
        ),
    ]


@pytest.fixture
def sample_mapping():
    return {
        "VTI": TickerMapping(asset_class="us_equity", similar_tickers=["ITOT", "SCHB", "SPTM"]),
        "VXUS": TickerMapping(asset_class="intl_equity", similar_tickers=["IXUS", "SCHF"]),
        "BND": TickerMapping(asset_class="bonds", similar_tickers=["AGG", "SCHZ"]),
        "SPAXX": TickerMapping(asset_class="cash"),
        "FXAIX": TickerMapping(asset_class="us_equity", similar_tickers=["VOO", "SPY", "IVV"]),
    }


@pytest.fixture
def sample_config():
    return RebalanceConfig(
        threshold_pct=Decimal("3.0"),
        min_trade_value=Decimal("50"),
        tlh_enabled=True,
        avoid_gains_in_taxable=True,
        cash_to_invest=Decimal("0"),
        account_mappings={
            "Individual": AccountType.TAXABLE,
            "ROTH IRA": AccountType.ROTH_IRA,
        },
    )
