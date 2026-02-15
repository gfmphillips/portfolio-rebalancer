import sys
from pathlib import Path

import typer
from rich.console import Console

app = typer.Typer(help="Portfolio rebalancer for Fidelity accounts.")
_console = Console(stderr=True)


def _validate_file(path_str: str, label: str) -> Path:
    """Validate that a file exists and return its Path."""
    p = Path(path_str)
    if not p.exists():
        _console.print(f"[red]Error:[/red] {label} not found: {p}")
        raise typer.Exit(1)
    if not p.is_file():
        _console.print(f"[red]Error:[/red] {label} is not a file: {p}")
        raise typer.Exit(1)
    return p


def _safe_load(label: str, loader, *args):
    """Call a loader function with user-friendly error handling."""
    try:
        return loader(*args)
    except ValueError as e:
        _console.print(f"[red]Validation error in {label}:[/red] {e}")
        raise typer.Exit(1)
    except Exception as e:
        _console.print(f"[red]Error loading {label}:[/red] {e}")
        raise typer.Exit(1)


@app.command()
def show(
    positions: str = typer.Option(..., help="Path to Fidelity positions CSV"),
    mapping: str = typer.Option(..., help="Path to ticker mapping YAML"),
):
    """Show current portfolio allocation."""
    from .config import load_mapping
    from .output import print_allocation_table
    from .parser import parse_fidelity_csv

    pos_path = _validate_file(positions, "Positions CSV")
    map_path = _validate_file(mapping, "Mapping YAML")

    positions_list = _safe_load("positions CSV", parse_fidelity_csv, pos_path)
    mapping_data = _safe_load("mapping YAML", load_mapping, map_path)
    print_allocation_table(positions_list, mapping_data)


@app.command()
def run(
    positions: str = typer.Option(..., help="Path to Fidelity positions CSV"),
    targets: str = typer.Option(None, help="Path to target allocation YAML (legacy format)"),
    mapping: str = typer.Option(..., help="Path to ticker mapping YAML"),
    config: str = typer.Option(..., help="Path to config YAML (unified or legacy)"),
    output: str = typer.Option(None, help="Path to write markdown report"),
    german_tax: bool = typer.Option(False, "--german-tax", help="Show German tax advisory annotations"),
    transactions: str = typer.Option(None, help="Path to transaction history CSV (for wash sale detection)"),
    lots: str = typer.Option(None, help="Path to tax lot CSV (for lot-aware selling)"),
):
    """Run portfolio rebalancing and show trade recommendations."""
    from .config import (
        is_unified_config,
        load_config,
        load_mapping,
        load_targets,
        load_unified_config,
    )
    from .engine import analyze_consolidation, build_run_metadata, rebalance
    from .german_tax import annotate_trades
    from .models import GermanTaxConfig, OutputConfig
    from .output import (
        filter_actionable_trades,
        print_consolidation_report,
        print_german_tax_section,
        print_rebalance_report,
        sort_trades,
        write_markdown_report,
    )
    from .parser import attach_lots, parse_fidelity_csv, parse_lots, parse_transactions

    pos_path = _validate_file(positions, "Positions CSV")
    map_path = _validate_file(mapping, "Mapping YAML")
    cfg_path = _validate_file(config, "Config YAML")

    # Load transaction history if provided
    recent_transactions = None
    if transactions is not None:
        txn_path = _validate_file(transactions, "Transactions CSV")
        recent_transactions = _safe_load("transactions CSV", parse_transactions, txn_path)

    positions_list = _safe_load("positions CSV", parse_fidelity_csv, pos_path)
    mapping_data = _safe_load("mapping YAML", load_mapping, map_path)

    # Parse and attach tax lots if provided
    lot_warnings: list[str] = []
    if lots is not None:
        lot_path = _validate_file(lots, "Tax lot CSV")
        lots_data = _safe_load("tax lot CSV", parse_lots, lot_path)
        lot_warnings = attach_lots(positions_list, lots_data)

    # Auto-detect unified vs legacy config format
    from decimal import Decimal

    from .models import ConstraintsConfig

    output_config = OutputConfig()
    german_tax_config = GermanTaxConfig()
    cash_config_fx = Decimal("1.10")
    constraints_config = ConstraintsConfig()
    if is_unified_config(cfg_path):
        uc = _safe_load("unified config YAML", load_unified_config, cfg_path)
        targets_data = uc.targets
        config_data = uc.rebalance_config
        output_config = uc.output_config
        german_tax_config = uc.german_tax_config
        constraints_config = uc.constraints_config
        cash_config_fx = uc.cash_config.eurusd_fx
    else:
        # Legacy format requires --targets
        if targets is None:
            _console.print(
                "[red]Error:[/red] --targets is required when using legacy config format."
            )
            raise typer.Exit(1)
        tgt_path = _validate_file(targets, "Targets YAML")
        targets_data = _safe_load("targets YAML", load_targets, tgt_path)
        config_data = _safe_load("config YAML", load_config, cfg_path)

    # CLI flag overrides config file
    if german_tax:
        german_tax_config.enabled = True

    metadata = build_run_metadata(eurusd_fx=cash_config_fx)
    result = rebalance(
        positions_list, targets_data, mapping_data, config_data,
        metadata=metadata, constraints=constraints_config,
        recent_transactions=recent_transactions,
    )

    # Add lot warnings to result
    for w in lot_warnings:
        result.warnings.append(w)

    # Apply sorting and filtering
    result.trades = sort_trades(result.trades, output_config.sort_order)
    result.trades = filter_actionable_trades(
        result.trades, config_data.min_trade_value, output_config.show_only_actionable_trades
    )

    print_rebalance_report(result, output_config)

    # German tax advisory section
    if german_tax_config.enabled:
        annotations = annotate_trades(result.trades, mapping_data, german_tax_config)
        print_german_tax_section(annotations, german_tax_config)

    # Consolidation report
    consolidation = analyze_consolidation(positions_list, mapping_data)
    if consolidation.legacy_value > 0:
        print_consolidation_report(consolidation)

    if output:
        write_markdown_report(result, Path(output), output_config)
