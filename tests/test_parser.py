import csv
import tempfile
from decimal import Decimal
from pathlib import Path

import pytest

from rebalancer.models import AccountType
from rebalancer.models import Position, TaxLot
from rebalancer.parser import (
    _clean_numeric,
    _detect_account_type,
    _fidelity_date_to_iso,
    _normalize_symbol,
    attach_lots,
    parse_fidelity_csv,
    parse_fidelity_lots_paste,
    parse_lots,
    parse_transactions,
)


class TestCleanNumeric:
    def test_simple_number(self):
        assert _clean_numeric("1234.56") == Decimal("1234.56")

    def test_dollar_sign(self):
        assert _clean_numeric("$1,234.56") == Decimal("1234.56")

    def test_negative(self):
        assert _clean_numeric("-$1,234.56") == Decimal("-1234.56")

    def test_parens_negative(self):
        assert _clean_numeric("($500.00)") == Decimal("-500.00")

    def test_empty(self):
        assert _clean_numeric("") == Decimal("0")

    def test_na(self):
        assert _clean_numeric("n/a") == Decimal("0")

    def test_dashes(self):
        assert _clean_numeric("--") == Decimal("0")

    def test_whitespace(self):
        assert _clean_numeric("  $1,000.00  ") == Decimal("1000.00")


class TestNormalizeSymbol:
    def test_plain(self):
        assert _normalize_symbol("VTI") == "VTI"

    def test_trailing_stars(self):
        assert _normalize_symbol("SPAXX**") == "SPAXX"

    def test_whitespace(self):
        assert _normalize_symbol("  VTI  ") == "VTI"


class TestDetectAccountType:
    def test_individual(self):
        assert _detect_account_type("Individual - TOD...XXX123") == AccountType.TAXABLE

    def test_roth_ira(self):
        assert _detect_account_type("ROTH IRA...XXX789") == AccountType.ROTH_IRA

    def test_traditional_ira(self):
        assert _detect_account_type("TRADITIONAL IRA...XXX456") == AccountType.TRADITIONAL_IRA

    def test_401k(self):
        assert _detect_account_type("401(K)...XXX321") == AccountType.FOUR_01K

    def test_roth_401k(self):
        assert _detect_account_type("ROTH 401(K)...XXX321") == AccountType.ROTH_401K

    def test_hsa(self):
        assert _detect_account_type("HSA...XXX111") == AccountType.HSA

    def test_default_taxable(self):
        assert _detect_account_type("Something Unknown") == AccountType.TAXABLE

    def test_custom_mapping(self):
        mappings = {"My Brokerage": AccountType.TAXABLE}
        assert _detect_account_type("My Brokerage Account", mappings) == AccountType.TAXABLE


class TestParseFidelityCSV:
    def test_parse_example_file(self, examples_dir):
        positions = parse_fidelity_csv(examples_dir / "fidelity_positions.csv")

        assert len(positions) == 8

        # Check first position (VTI in taxable)
        vti = positions[0]
        assert vti.ticker == "VTI"
        assert vti.account_type == AccountType.TAXABLE
        assert vti.quantity == Decimal("100")
        assert vti.price == Decimal("250.00")
        assert vti.market_value == Decimal("25000.00")
        assert vti.cost_basis_total == Decimal("20000.00")

        # Check SPAXX has stars stripped
        spaxx = positions[3]
        assert spaxx.ticker == "SPAXX"
        assert spaxx.market_value == Decimal("2400.00")

        # Check Roth IRA account detection
        fxaix = positions[4]
        assert fxaix.ticker == "FXAIX"
        assert fxaix.account_type == AccountType.ROTH_IRA
        assert fxaix.market_value == Decimal("10000.00")

    def test_total_value(self, examples_dir):
        positions = parse_fidelity_csv(examples_dir / "fidelity_positions.csv")
        total = sum(p.market_value for p in positions)
        assert total == Decimal("61000.00")

    def test_empty_csv(self, tmp_path):
        csv_file = tmp_path / "empty.csv"
        csv_file.write_text("")
        positions = parse_fidelity_csv(csv_file)
        assert positions == []

    def test_header_only_csv(self, tmp_path):
        csv_file = tmp_path / "header_only.csv"
        csv_file.write_text(
            "Account Name/Number,Symbol,Description,Quantity,Last Price,"
            "Current Value,Today's Gain/Loss Dollar,Today's Gain/Loss Percent,"
            "Total Gain/Loss Dollar,Total Gain/Loss Percent,Percent Of Account,"
            "Cost Basis Total,Average Cost Basis,Type\n"
        )
        positions = parse_fidelity_csv(csv_file)
        assert positions == []

    def test_with_account_mappings(self, examples_dir):
        mappings = {
            "Individual": AccountType.TAXABLE,
            "ROTH IRA": AccountType.ROTH_IRA,
        }
        positions = parse_fidelity_csv(examples_dir / "fidelity_positions.csv", mappings)
        taxable = [p for p in positions if p.account_type == AccountType.TAXABLE]
        roth = [p for p in positions if p.account_type == AccountType.ROTH_IRA]
        assert len(taxable) == 4
        assert len(roth) == 4


class TestParseTransactions:
    def test_simplified_format(self, tmp_path):
        csv_content = "Date,Account,Ticker,Action,Shares\n2024-12-15,Individual,VTI,BUY,25.0\n2024-12-20,ROTH IRA,VXUS,BUY,10.0\n"
        path = tmp_path / "txns.csv"
        path.write_text(csv_content)
        txns = parse_transactions(path)
        assert len(txns) == 2
        assert txns[0].ticker == "VTI"
        assert txns[0].action == "BUY"
        assert txns[0].shares == Decimal("25.0")
        assert txns[0].account_name == "Individual"
        assert txns[1].ticker == "VXUS"

    def test_sell_action(self, tmp_path):
        csv_content = "Date,Account,Ticker,Action,Shares\n2024-12-15,Individual,VTI,SELL,10.0\n"
        path = tmp_path / "txns.csv"
        path.write_text(csv_content)
        txns = parse_transactions(path)
        assert len(txns) == 1
        assert txns[0].action == "SELL"

    def test_fidelity_format(self, tmp_path):
        csv_content = "Run Date,Account,Action,Symbol,Description,Type,Quantity,Price,Commission,Fees,Amount\n12/15/2024,Individual - TOD,YOU BOUGHT,VTI,VANGUARD TOTAL STOCK,Cash,25.0,250.00,0,0,6250.00\n"
        path = tmp_path / "txns.csv"
        path.write_text(csv_content)
        txns = parse_transactions(path)
        assert len(txns) == 1
        assert txns[0].ticker == "VTI"
        assert txns[0].action == "BUY"
        assert txns[0].shares == Decimal("25.0")

    def test_dividend_reinvestment_is_buy(self, tmp_path):
        csv_content = "Date,Account,Ticker,Action,Shares\n2024-12-15,Individual,VTI,REINVESTMENT,0.5\n"
        path = tmp_path / "txns.csv"
        path.write_text(csv_content)
        txns = parse_transactions(path)
        assert len(txns) == 1
        assert txns[0].action == "BUY"

    def test_skips_unknown_actions(self, tmp_path):
        csv_content = "Date,Account,Ticker,Action,Shares\n2024-12-15,Individual,VTI,TRANSFER,25.0\n"
        path = tmp_path / "txns.csv"
        path.write_text(csv_content)
        txns = parse_transactions(path)
        assert len(txns) == 0

    def test_skips_zero_shares(self, tmp_path):
        csv_content = "Date,Account,Ticker,Action,Shares\n2024-12-15,Individual,VTI,BUY,0\n"
        path = tmp_path / "txns.csv"
        path.write_text(csv_content)
        txns = parse_transactions(path)
        assert len(txns) == 0

    def test_empty_file_raises(self, tmp_path):
        path = tmp_path / "empty.csv"
        path.write_text("")
        with pytest.raises(ValueError, match="Could not find"):
            parse_transactions(path)

    def test_unrecognized_format_raises(self, tmp_path):
        csv_content = "Foo,Bar,Baz\n1,2,3\n"
        path = tmp_path / "bad.csv"
        path.write_text(csv_content)
        with pytest.raises(ValueError, match="Could not find"):
            parse_transactions(path)


class TestParseLots:
    def test_basic_parse(self, tmp_path):
        csv_content = (
            "Account,Ticker,AcquisitionDate,Shares,CostBasisPerShare\n"
            "Individual,VTI,2020-03-15,50,180.00\n"
            "Individual,VTI,2021-06-01,30,220.00\n"
        )
        path = tmp_path / "lots.csv"
        path.write_text(csv_content)
        lots = parse_lots(path)
        assert ("Individual", "VTI") in lots
        assert len(lots[("Individual", "VTI")]) == 2
        assert lots[("Individual", "VTI")][0].shares == Decimal("50")
        assert lots[("Individual", "VTI")][0].cost_basis_per_share == Decimal("180.00")
        assert lots[("Individual", "VTI")][1].acquisition_date == "2021-06-01"

    def test_dollar_and_comma_in_cost(self, tmp_path):
        csv_content = (
            "Account,Ticker,AcquisitionDate,Shares,CostBasisPerShare\n"
            'Acct1,VXUS,2020-01-01,100,"$1,234.56"\n'
        )
        path = tmp_path / "lots.csv"
        path.write_text(csv_content)
        lots = parse_lots(path)
        assert lots[("Acct1", "VXUS")][0].cost_basis_per_share == Decimal("1234.56")

    def test_strip_stars_from_ticker(self, tmp_path):
        csv_content = (
            "Account,Ticker,AcquisitionDate,Shares,CostBasisPerShare\n"
            "Acct1,SPAXX**,2020-01-01,100,1.00\n"
        )
        path = tmp_path / "lots.csv"
        path.write_text(csv_content)
        lots = parse_lots(path)
        assert ("Acct1", "SPAXX") in lots

    def test_multiple_accounts_same_ticker(self, tmp_path):
        csv_content = (
            "Account,Ticker,AcquisitionDate,Shares,CostBasisPerShare\n"
            "Acct1,VTI,2020-01-01,50,180.00\n"
            "Acct2,VTI,2021-01-01,30,200.00\n"
        )
        path = tmp_path / "lots.csv"
        path.write_text(csv_content)
        lots = parse_lots(path)
        assert len(lots[("Acct1", "VTI")]) == 1
        assert len(lots[("Acct2", "VTI")]) == 1

    def test_empty_file_raises(self, tmp_path):
        path = tmp_path / "empty.csv"
        path.write_text("")
        with pytest.raises(ValueError, match="Could not find"):
            parse_lots(path)

    def test_bad_headers_raises(self, tmp_path):
        csv_content = "Foo,Bar,Baz\n1,2,3\n"
        path = tmp_path / "bad.csv"
        path.write_text(csv_content)
        with pytest.raises(ValueError, match="Could not find"):
            parse_lots(path)


class TestAttachLots:
    def _make_position(self, account, ticker, quantity):
        return Position(
            account_name=account,
            account_type=AccountType.TAXABLE,
            ticker=ticker,
            description="Test",
            quantity=Decimal(str(quantity)),
            price=Decimal("100"),
            market_value=Decimal(str(quantity)) * Decimal("100"),
        )

    def test_exact_match(self):
        pos = self._make_position("Acct1", "VTI", 80)
        lots = {
            ("Acct1", "VTI"): [
                TaxLot(acquisition_date="2020-01-01", shares=Decimal("50"), cost_basis_per_share=Decimal("180")),
                TaxLot(acquisition_date="2021-01-01", shares=Decimal("30"), cost_basis_per_share=Decimal("220")),
            ]
        }
        warnings = attach_lots([pos], lots)
        assert len(pos.tax_lots) == 2
        assert warnings == []

    def test_fuzzy_match_substring(self):
        pos = self._make_position("Individual - TOD...XXX123", "VTI", 50)
        lots = {
            ("Individual", "VTI"): [
                TaxLot(acquisition_date="2020-01-01", shares=Decimal("50"), cost_basis_per_share=Decimal("180")),
            ]
        }
        warnings = attach_lots([pos], lots)
        assert len(pos.tax_lots) == 1

    def test_mismatch_warns(self):
        pos = self._make_position("Acct1", "VTI", 80)
        lots = {
            ("Acct1", "VTI"): [
                TaxLot(acquisition_date="2020-01-01", shares=Decimal("50"), cost_basis_per_share=Decimal("180")),
            ]
        }
        warnings = attach_lots([pos], lots)
        assert len(warnings) == 1
        assert "differ" in warnings[0]

    def test_unmatched_position_keeps_empty(self):
        pos = self._make_position("Acct1", "VTI", 50)
        lots = {
            ("Acct2", "VXUS"): [
                TaxLot(acquisition_date="2020-01-01", shares=Decimal("50"), cost_basis_per_share=Decimal("60")),
            ]
        }
        warnings = attach_lots([pos], lots)
        assert pos.tax_lots == []
        assert warnings == []


class TestFidelityDateToIso:
    def test_basic(self):
        assert _fidelity_date_to_iso("Mar-04-2025") == "2025-03-04"

    def test_december(self):
        assert _fidelity_date_to_iso("Dec-10-2025") == "2025-12-10"

    def test_january(self):
        assert _fidelity_date_to_iso("Jan-01-2020") == "2020-01-01"


class TestParseFidelityLotsPaste:
    SINGLE_LOT_PASTE = """
Account:
Rollover IRA

ETFs
1 positions

BSV
VANGUARD SHORT-TERM BOND ETF

Buy
Sell
Set exit plan
Purchase history
Research

Acquired
Term
$ Total gain/loss
% Total gain/loss
Current value
Quantity
Average cost basis
Cost basis total
Dec-10-2025
Short
+$41.10
+0.34%
$12,004.96
152
$78.71
$11,963.86
"""

    def test_single_account_single_ticker_single_lot(self):
        lots = parse_fidelity_lots_paste(self.SINGLE_LOT_PASTE)
        assert ("Rollover IRA", "BSV") in lots
        lot_list = lots[("Rollover IRA", "BSV")]
        assert len(lot_list) == 1
        assert lot_list[0].acquisition_date == "2025-12-10"
        assert lot_list[0].shares == Decimal("152")
        assert lot_list[0].cost_basis_per_share == Decimal("78.71")

    def test_multi_lot_multi_ticker(self):
        text = """
Account:
Rollover IRA

BSV
VANGUARD SHORT-TERM BOND ETF

Purchase history
Research

Acquired
Term
$ Total gain/loss
% Total gain/loss
Current value
Quantity
Average cost basis
Cost basis total
Dec-10-2025
Short
+$41.10
+0.34%
$12,004.96
152
$78.71
$11,963.86
Mar-04-2025
Long
+$100.00
+1.00%
$5,000.00
60.5
$81.00
$4,900.00

VCSH
VANGUARD SHORT-TERM CORP BOND ETF

Purchase history
Research

Acquired
Term
$ Total gain/loss
% Total gain/loss
Current value
Quantity
Average cost basis
Cost basis total
Jan-15-2024
Long
+$200.00
+2.00%
$10,200.00
200
$50.00
$10,000.00
"""
        lots = parse_fidelity_lots_paste(text)
        assert ("Rollover IRA", "BSV") in lots
        assert len(lots[("Rollover IRA", "BSV")]) == 2
        assert lots[("Rollover IRA", "BSV")][0].shares == Decimal("152")
        assert lots[("Rollover IRA", "BSV")][1].shares == Decimal("60.5")

        assert ("Rollover IRA", "VCSH") in lots
        assert len(lots[("Rollover IRA", "VCSH")]) == 1
        assert lots[("Rollover IRA", "VCSH")][0].acquisition_date == "2024-01-15"

    def test_multi_account(self):
        text = """
Account:
Rollover IRA

BSV
VANGUARD SHORT-TERM BOND ETF

Purchase history

Acquired
Term
$ Total gain/loss
% Total gain/loss
Current value
Quantity
Average cost basis
Cost basis total
Dec-10-2025
Short
+$41.10
+0.34%
$12,004.96
152
$78.71
$11,963.86

Account:
Individual

VTI
VANGUARD TOTAL STOCK MKT ETF

Purchase history

Acquired
Term
$ Total gain/loss
% Total gain/loss
Current value
Quantity
Average cost basis
Cost basis total
Jun-01-2021
Long
+$500.00
+5.00%
$10,500.00
40
$250.00
$10,000.00
"""
        lots = parse_fidelity_lots_paste(text)
        assert ("Rollover IRA", "BSV") in lots
        assert ("Individual", "VTI") in lots
        assert lots[("Individual", "VTI")][0].shares == Decimal("40")

    def test_fractional_shares(self):
        text = """
Account:
Roth IRA

VTI
VANGUARD TOTAL STOCK MKT ETF

Purchase history

Acquired
Term
$ Total gain/loss
% Total gain/loss
Current value
Quantity
Average cost basis
Cost basis total
Mar-04-2025
Short
+$10.00
+0.50%
$1,210.00
4.803
$250.00
$1,200.75
"""
        lots = parse_fidelity_lots_paste(text)
        assert lots[("Roth IRA", "VTI")][0].shares == Decimal("4.803")

    def test_edit_cost_basis_before_header(self):
        text = """
Account:
Rollover IRA

BSV
VANGUARD SHORT-TERM BOND ETF

Purchase history
Research

[Edit Cost Basis]
Acquired
Term
$ Total gain/loss
% Total gain/loss
Current value
Quantity
Average cost basis
Cost basis total
Dec-10-2025
Short
+$41.10
+0.34%
$12,004.96
152
$78.71
$11,963.86
"""
        lots = parse_fidelity_lots_paste(text)
        assert ("Rollover IRA", "BSV") in lots
        assert lots[("Rollover IRA", "BSV")][0].shares == Decimal("152")

    def test_skips_positions_without_lots(self):
        text = """
Account:
Rollover IRA

BSV
VANGUARD SHORT-TERM BOND ETF
$12,004.96
152 shares

VCSH
VANGUARD SHORT-TERM CORP BOND ETF

Purchase history

Acquired
Term
$ Total gain/loss
% Total gain/loss
Current value
Quantity
Average cost basis
Cost basis total
Jan-15-2024
Long
+$200.00
+2.00%
$10,200.00
200
$50.00
$10,000.00
"""
        lots = parse_fidelity_lots_paste(text)
        # BSV has no lot header, so it's skipped
        assert ("Rollover IRA", "BSV") not in lots
        assert ("Rollover IRA", "VCSH") in lots

    def test_account_name_cleanup_trailing_id(self):
        text = """
Account:
Rollover IRAx02

BSV
VANGUARD SHORT-TERM BOND ETF

Purchase history

Acquired
Term
$ Total gain/loss
% Total gain/loss
Current value
Quantity
Average cost basis
Cost basis total
Dec-10-2025
Short
+$41.10
+0.34%
$12,004.96
152
$78.71
$11,963.86
"""
        lots = parse_fidelity_lots_paste(text)
        assert ("Rollover IRA", "BSV") in lots

    def test_empty_text(self):
        lots = parse_fidelity_lots_paste("")
        assert lots == {}

    def test_no_lots_text(self):
        lots = parse_fidelity_lots_paste("Some random text\nwith no lot data")
        assert lots == {}

    def test_integration_paste_to_attach(self):
        """End-to-end: paste -> parse -> attach_lots -> positions have lots."""
        text = """
Account:
Rollover IRA

BSV
VANGUARD SHORT-TERM BOND ETF

Purchase history

Acquired
Term
$ Total gain/loss
% Total gain/loss
Current value
Quantity
Average cost basis
Cost basis total
Dec-10-2025
Short
+$41.10
+0.34%
$12,004.96
100
$78.71
$7,871.00
Mar-04-2025
Long
+$50.00
+1.00%
$5,050.00
52
$96.15
$4,999.80
"""
        lots = parse_fidelity_lots_paste(text)
        pos = Position(
            account_name="Rollover IRA",
            account_type=AccountType.TRADITIONAL_IRA,
            ticker="BSV",
            description="VANGUARD SHORT-TERM BOND ETF",
            quantity=Decimal("152"),
            price=Decimal("78.71"),
            market_value=Decimal("12004.96"),
        )
        warnings = attach_lots([pos], lots)
        assert len(pos.tax_lots) == 2
        assert pos.tax_lots[0].acquisition_date == "2025-12-10"
        assert pos.tax_lots[1].acquisition_date == "2025-03-04"
        assert warnings == []
