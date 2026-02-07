import csv
import tempfile
from decimal import Decimal
from pathlib import Path

import pytest

from rebalancer.models import AccountType
from rebalancer.parser import (
    _clean_numeric,
    _detect_account_type,
    _normalize_symbol,
    parse_fidelity_csv,
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
