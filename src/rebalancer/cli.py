import typer

app = typer.Typer(help="Portfolio rebalancer for Fidelity accounts.")


@app.command()
def show(
    positions: str = typer.Option(..., help="Path to Fidelity positions CSV"),
    mapping: str = typer.Option(..., help="Path to ticker mapping YAML"),
):
    """Show current portfolio allocation."""
    from pathlib import Path

    from .config import load_mapping
    from .output import print_allocation_table
    from .parser import parse_fidelity_csv

    positions_list = parse_fidelity_csv(Path(positions))
    mapping_data = load_mapping(Path(mapping))
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
    from pathlib import Path

    from .config import load_config, load_mapping, load_targets
    from .engine import rebalance
    from .output import print_rebalance_report, write_markdown_report
    from .parser import parse_fidelity_csv

    positions_list = parse_fidelity_csv(Path(positions))
    targets_data = load_targets(Path(targets))
    mapping_data = load_mapping(Path(mapping))
    config_data = load_config(Path(config))

    result = rebalance(positions_list, targets_data, mapping_data, config_data)
    print_rebalance_report(result)

    if output:
        write_markdown_report(result, Path(output))
