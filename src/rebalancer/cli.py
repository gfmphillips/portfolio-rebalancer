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
    targets: str = typer.Option(..., help="Path to target allocation YAML"),
    mapping: str = typer.Option(..., help="Path to ticker mapping YAML"),
    config: str = typer.Option(..., help="Path to config YAML"),
    output: str = typer.Option(None, help="Path to write markdown report"),
):
    """Run portfolio rebalancing and show trade recommendations."""
    from .config import load_config, load_mapping, load_targets
    from .engine import rebalance
    from .output import print_rebalance_report, write_markdown_report
    from .parser import parse_fidelity_csv

    pos_path = _validate_file(positions, "Positions CSV")
    tgt_path = _validate_file(targets, "Targets YAML")
    map_path = _validate_file(mapping, "Mapping YAML")
    cfg_path = _validate_file(config, "Config YAML")

    positions_list = _safe_load("positions CSV", parse_fidelity_csv, pos_path)
    targets_data = _safe_load("targets YAML", load_targets, tgt_path)
    mapping_data = _safe_load("mapping YAML", load_mapping, map_path)
    config_data = _safe_load("config YAML", load_config, cfg_path)

    result = rebalance(positions_list, targets_data, mapping_data, config_data)
    print_rebalance_report(result)

    if output:
        write_markdown_report(result, Path(output))
