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
            "include_in_portfolio": True,
            "external_cash_eur": 0,
            "external_cash_usd": 0,
            "eurusd_fx": 1.10,
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
        targets, config, output_config, cash_config, _gt = load_unified_config(unified_config_path)

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
        targets, config, _, _, _ = load_unified_config(unified_config_path)
        assert config.tlh_enabled is False
        assert config.avoid_gains_in_taxable is False

    def test_tax_enabled_true(self, tmp_path):
        data = {
            "allocation": {"us_equity": 60, "bonds": 40},
            "tax": {"enabled": True},
        }
        path = tmp_path / "tax_on.yaml"
        path.write_text(yaml.dump(data))
        _, config, _, _, _ = load_unified_config(path)
        assert config.tlh_enabled is True
        assert config.avoid_gains_in_taxable is True

    def test_account_mappings_parsed(self, unified_config_path):
        _, config, _, _, _ = load_unified_config(unified_config_path)
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
        targets, config, output_config, cash_config, _gt = load_unified_config(path)

        assert len(targets) == 2
        assert config.tlh_enabled is False
        assert cash_config.include_in_portfolio is True
        assert cash_config.eurusd_fx == Decimal("1.10")
        assert output_config.show_only_actionable_trades is True
        assert output_config.sort_order == [SortKey.SELLS_FIRST, SortKey.LARGEST_TRADE_FIRST]
        assert output_config.precision.currency == 0
        assert output_config.precision.pct == 2

    def test_output_sort_order_parsed(self, unified_config_path):
        _, _, output_config, _, _ = load_unified_config(unified_config_path)
        assert output_config.sort_order == [SortKey.SELLS_FIRST, SortKey.LARGEST_TRADE_FIRST]

    def test_example_unified_config(self, examples_dir):
        """The example unified_config.yaml should load successfully."""
        path = examples_dir / "unified_config.yaml"
        if not path.exists():
            pytest.skip("unified_config.yaml not in examples/")
        targets, config, output_config, cash_config, _gt = load_unified_config(path)
        assert len(targets) == 5
        total = sum(t.target_pct for t in targets)
        assert total == Decimal("100")


class TestEndToEndUnifiedConfig:
    def test_rebalance_with_unified_config(self, examples_dir):
        """E2E: load unified config and run rebalance."""
        from rebalancer.config import load_mapping
        from rebalancer.engine import rebalance
        from rebalancer.parser import parse_fidelity_csv

        positions = parse_fidelity_csv(examples_dir / "fidelity_positions.csv")
        mapping = load_mapping(examples_dir / "mapping.yaml")
        targets, config, output_config, cash_config, _gt = load_unified_config(
            examples_dir / "unified_config.yaml"
        )

        result = rebalance(positions, targets, mapping, config)
        assert result.total_portfolio_value == Decimal("61000.00")
        assert len(result.current_allocation) >= 4
