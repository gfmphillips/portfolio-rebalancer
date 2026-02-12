from decimal import Decimal
from pathlib import Path

import pytest
import yaml

from rebalancer.config import is_unified_config, load_unified_config
from rebalancer.models import AccountType, SortKey


@pytest.fixture
def unified_config_path(tmp_path):
    """Write a full unified config to a temp file and return its path."""
    data = {
        "allocation": {
            "cash": 10,
            "bonds": 20,
            "reit": 5,
            "us_equity": 45,
            "intl_equity": 20,
        },
        "rebalance": {
            "threshold_pct": 5.0,
            "min_trade_value": 500,
        },
        "cash": {
            "eurusd_fx": 1.10,
            "investable": {"eur": 0, "usd": 0},
            "emergency": {"eur": 0, "usd": 0},
        },
        "tax": {"enabled": False},
        "accounts": {
            "Individual": "taxable",
            "ROTH": "roth_ira",
            "401(K)": "401k",
        },
        "output": {
            "show_only_actionable_trades": True,
            "sort_order": ["sells_first", "largest_trade_first"],
            "precision": {"currency": 0, "pct": 2},
        },
    }
    path = tmp_path / "unified.yaml"
    path.write_text(yaml.dump(data))
    return path


class TestIsUnifiedConfig:
    def test_unified_format_detected(self, unified_config_path):
        assert is_unified_config(unified_config_path) is True

    def test_legacy_format_detected(self, examples_dir):
        assert is_unified_config(examples_dir / "config.yaml") is False


class TestLoadUnifiedConfig:
    def test_parses_all_sections(self, unified_config_path):
        targets, config, output_config, cash_config, _gt, _cst = load_unified_config(unified_config_path)

        assert len(targets) == 5
        assert config.threshold_pct == Decimal("5.0")
        assert config.min_trade_value == Decimal("500")
        assert output_config.show_only_actionable_trades is True
        assert cash_config.eurusd_fx == Decimal("1.10")

    def test_allocation_sum_must_be_100(self, tmp_path):
        data = {"allocation": {"us_equity": 50, "bonds": 40}}
        path = tmp_path / "bad_alloc.yaml"
        path.write_text(yaml.dump(data))
        with pytest.raises(ValueError, match="sum to 100"):
            load_unified_config(path)

    def test_tax_enabled_false(self, unified_config_path):
        targets, config, _, _, _, _ = load_unified_config(unified_config_path)
        assert config.tlh_enabled is False
        assert config.avoid_gains_in_taxable is False

    def test_tax_enabled_true(self, tmp_path):
        data = {
            "allocation": {"us_equity": 60, "bonds": 40},
            "tax": {"enabled": True},
        }
        path = tmp_path / "tax_on.yaml"
        path.write_text(yaml.dump(data))
        _, config, _, _, _, _ = load_unified_config(path)
        assert config.tlh_enabled is True
        assert config.avoid_gains_in_taxable is True

    def test_tax_tlh_override(self, tmp_path):
        """tlh_enabled can override tax.enabled."""
        data = {
            "allocation": {"us_equity": 60, "bonds": 40},
            "tax": {"enabled": False, "tlh_enabled": True},
        }
        path = tmp_path / "tlh_override.yaml"
        path.write_text(yaml.dump(data))
        _, config, _, _, _, _ = load_unified_config(path)
        assert config.tlh_enabled is True
        assert config.avoid_gains_in_taxable is False

    def test_tax_avoid_gains_override(self, tmp_path):
        """avoid_gains_in_taxable can override tax.enabled."""
        data = {
            "allocation": {"us_equity": 60, "bonds": 40},
            "tax": {"enabled": True, "avoid_gains_in_taxable": False},
        }
        path = tmp_path / "avoid_gains_override.yaml"
        path.write_text(yaml.dump(data))
        _, config, _, _, _, _ = load_unified_config(path)
        assert config.tlh_enabled is True
        assert config.avoid_gains_in_taxable is False

    def test_tax_both_overrides(self, tmp_path):
        """Both tlh_enabled and avoid_gains_in_taxable can override."""
        data = {
            "allocation": {"us_equity": 60, "bonds": 40},
            "tax": {"enabled": False, "tlh_enabled": True, "avoid_gains_in_taxable": True},
        }
        path = tmp_path / "both_override.yaml"
        path.write_text(yaml.dump(data))
        _, config, _, _, _, _ = load_unified_config(path)
        assert config.tlh_enabled is True
        assert config.avoid_gains_in_taxable is True

    def test_account_mappings_parsed(self, unified_config_path):
        _, config, _, _, _, _ = load_unified_config(unified_config_path)
        assert config.account_mappings["Individual"] == AccountType.TAXABLE
        assert config.account_mappings["ROTH"] == AccountType.ROTH_IRA
        assert config.account_mappings["401(K)"] == AccountType.FOUR_01K

    def test_invalid_account_type_raises(self, tmp_path):
        data = {
            "allocation": {"us_equity": 60, "bonds": 40},
            "accounts": {"Bad": "invalid_type"},
        }
        path = tmp_path / "bad_acct.yaml"
        path.write_text(yaml.dump(data))
        with pytest.raises(ValueError, match="Invalid account type"):
            load_unified_config(path)

    def test_defaults_when_sections_omitted(self, tmp_path):
        data = {"allocation": {"us_equity": 60, "bonds": 40}}
        path = tmp_path / "minimal.yaml"
        path.write_text(yaml.dump(data))
        targets, config, output_config, cash_config, _gt, _cst = load_unified_config(path)

        assert len(targets) == 2
        assert config.tlh_enabled is False
        assert cash_config.eurusd_fx == Decimal("1.10")
        assert cash_config.investable.eur == Decimal("0")
        assert cash_config.investable.usd == Decimal("0")
        assert output_config.show_only_actionable_trades is True
        assert output_config.sort_order == [SortKey.SELLS_FIRST, SortKey.LARGEST_TRADE_FIRST]
        assert output_config.precision.currency == 0
        assert output_config.precision.pct == 2

    def test_output_sort_order_parsed(self, unified_config_path):
        _, _, output_config, _, _, _ = load_unified_config(unified_config_path)
        assert output_config.sort_order == [SortKey.SELLS_FIRST, SortKey.LARGEST_TRADE_FIRST]

    def test_example_unified_config(self, examples_dir):
        """The example unified_config.yaml should load successfully."""
        path = examples_dir / "unified_config.yaml"
        if not path.exists():
            pytest.skip("unified_config.yaml not in examples/")
        targets, config, output_config, cash_config, _gt, _cst = load_unified_config(path)
        assert len(targets) == 3
        total = sum(t.target_pct for t in targets)
        assert total == Decimal("100")


class TestCashConfigParsing:
    def test_new_format_investable_and_emergency(self, tmp_path):
        data = {
            "allocation": {"us_equity": 60, "bonds": 40},
            "cash": {
                "eurusd_fx": 1.10,
                "investable": {"eur": 2000, "usd": 1000},
                "emergency": {"eur": 10000, "usd": 5000},
            },
        }
        path = tmp_path / "new_cash.yaml"
        path.write_text(yaml.dump(data))
        _, _, _, cash_config, _, _ = load_unified_config(path)
        assert cash_config.investable.eur == Decimal("2000")
        assert cash_config.investable.usd == Decimal("1000")
        assert cash_config.emergency.eur == Decimal("10000")
        assert cash_config.emergency.usd == Decimal("5000")

    def test_legacy_format_include_true(self, tmp_path):
        data = {
            "allocation": {"us_equity": 60, "bonds": 40},
            "cash": {
                "include_in_portfolio": True,
                "external_cash_eur": 2000,
                "external_cash_usd": 1000,
                "eurusd_fx": 1.10,
            },
        }
        path = tmp_path / "legacy_cash.yaml"
        path.write_text(yaml.dump(data))
        import warnings as w
        with w.catch_warnings(record=True) as caught:
            w.simplefilter("always")
            _, _, _, cash_config, _, _ = load_unified_config(path)
        assert any("deprecated" in str(c.message).lower() for c in caught)
        assert cash_config.investable.eur == Decimal("2000")
        assert cash_config.investable.usd == Decimal("1000")
        assert cash_config.emergency.eur == Decimal("0")
        assert cash_config.emergency.usd == Decimal("0")

    def test_legacy_format_include_false(self, tmp_path):
        data = {
            "allocation": {"us_equity": 60, "bonds": 40},
            "cash": {
                "include_in_portfolio": False,
                "external_cash_eur": 5000,
                "external_cash_usd": 0,
                "eurusd_fx": 1.10,
            },
        }
        path = tmp_path / "legacy_cash_excl.yaml"
        path.write_text(yaml.dump(data))
        import warnings as w
        with w.catch_warnings(record=True):
            w.simplefilter("always")
            _, _, _, cash_config, _, _ = load_unified_config(path)
        assert cash_config.emergency.eur == Decimal("5000")
        assert cash_config.investable.eur == Decimal("0")

    def test_empty_cash_section(self, tmp_path):
        data = {
            "allocation": {"us_equity": 60, "bonds": 40},
            "cash": {"eurusd_fx": 1.15},
        }
        path = tmp_path / "empty_cash.yaml"
        path.write_text(yaml.dump(data))
        _, _, _, cash_config, _, _ = load_unified_config(path)
        assert cash_config.eurusd_fx == Decimal("1.15")
        assert cash_config.investable.eur == Decimal("0")
        assert cash_config.emergency.eur == Decimal("0")


class TestLoadMappingNewFields:
    def test_german_fund_category_parsed(self, tmp_path):
        data = {
            "VTI": {
                "asset_class": "us_equity",
                "german_fund_category": "aktienfonds",
                "is_accumulating": False,
            }
        }
        path = tmp_path / "mapping.yaml"
        path.write_text(yaml.dump(data))
        from rebalancer.config import load_mapping
        mapping = load_mapping(path)
        assert mapping["VTI"].german_fund_category == "aktienfonds"
        assert mapping["VTI"].is_accumulating is False

    def test_german_fields_default_none(self, tmp_path):
        data = {"VTI": {"asset_class": "us_equity"}}
        path = tmp_path / "mapping.yaml"
        path.write_text(yaml.dump(data))
        from rebalancer.config import load_mapping
        mapping = load_mapping(path)
        assert mapping["VTI"].german_fund_category is None
        assert mapping["VTI"].is_accumulating is None

    def test_is_accumulating_true(self, tmp_path):
        data = {
            "IWDA": {
                "asset_class": "intl_equity",
                "domicile": "IE",
                "is_accumulating": True,
                "german_fund_category": "aktienfonds",
            }
        }
        path = tmp_path / "mapping.yaml"
        path.write_text(yaml.dump(data))
        from rebalancer.config import load_mapping
        mapping = load_mapping(path)
        assert mapping["IWDA"].is_accumulating is True
        assert mapping["IWDA"].german_fund_category == "aktienfonds"


class TestConstraintsConfigParsing:
    def test_constraints_parsed(self, tmp_path):
        data = {
            "allocation": {"us_equity": 60, "bonds": 40},
            "constraints": {"min_taxable_bonds_usd": 10000},
        }
        path = tmp_path / "with_constraints.yaml"
        path.write_text(yaml.dump(data))
        _, _, _, _, _, constraints = load_unified_config(path)
        assert constraints.min_taxable_bonds_usd == Decimal("10000")

    def test_no_constraints_section(self, tmp_path):
        data = {"allocation": {"us_equity": 60, "bonds": 40}}
        path = tmp_path / "no_constraints.yaml"
        path.write_text(yaml.dump(data))
        _, _, _, _, _, constraints = load_unified_config(path)
        assert constraints.min_taxable_bonds_usd is None


class TestEndToEndUnifiedConfig:
    def test_rebalance_with_unified_config(self, examples_dir):
        """E2E: load unified config and run rebalance."""
        from rebalancer.config import load_mapping
        from rebalancer.engine import rebalance
        from rebalancer.parser import parse_fidelity_csv

        positions = parse_fidelity_csv(examples_dir / "fidelity_positions.csv")
        mapping = load_mapping(examples_dir / "mapping.yaml")
        targets, config, output_config, cash_config, _gt, _cst = load_unified_config(
            examples_dir / "unified_config.yaml"
        )

        result = rebalance(positions, targets, mapping, config)
        assert result.total_portfolio_value == Decimal("61000.00")
        assert len(result.current_allocation) >= 4
