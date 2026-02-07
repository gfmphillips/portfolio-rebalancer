from decimal import Decimal
from pathlib import Path

from rich.console import Console
from rich.table import Table

from .models import Position, RebalanceResult, TickerMapping

console = Console()


def _compute_allocation(
    positions: list[Position], mapping: dict[str, TickerMapping]
) -> tuple[Decimal, dict[str, Decimal], dict[str, Decimal]]:
    """Compute current allocation percentages by asset class.

    Returns (total_value, value_by_class, pct_by_class).
    """
    total_value = sum(p.market_value for p in positions)
    value_by_class: dict[str, Decimal] = {}

    for p in positions:
        ticker_info = mapping.get(p.ticker)
        asset_class = ticker_info.asset_class if ticker_info else "unmapped"
        value_by_class[asset_class] = value_by_class.get(
            asset_class, Decimal("0")
        ) + p.market_value

    pct_by_class: dict[str, Decimal] = {}
    if total_value > 0:
        for cls, val in value_by_class.items():
            pct_by_class[cls] = (val / total_value * Decimal("100")).quantize(
                Decimal("0.01")
            )

    return total_value, value_by_class, pct_by_class


def print_allocation_table(
    positions: list[Position], mapping: dict[str, TickerMapping]
) -> None:
    """Print a rich table showing current portfolio allocation."""
    total_value, value_by_class, pct_by_class = _compute_allocation(positions, mapping)

    # Positions by account
    accounts: dict[str, list[Position]] = {}
    for p in positions:
        accounts.setdefault(p.account_name, []).append(p)

    table = Table(title="Current Positions")
    table.add_column("Account", style="cyan")
    table.add_column("Ticker", style="bold")
    table.add_column("Description")
    table.add_column("Shares", justify="right")
    table.add_column("Price", justify="right")
    table.add_column("Value", justify="right", style="green")
    table.add_column("Asset Class", style="magenta")

    for account_name, acct_positions in accounts.items():
        for i, p in enumerate(acct_positions):
            ticker_info = mapping.get(p.ticker)
            asset_class = ticker_info.asset_class if ticker_info else "unmapped"
            table.add_row(
                account_name if i == 0 else "",
                p.ticker,
                p.description,
                str(p.quantity) if p.quantity else "",
                f"${p.price:,.2f}" if p.price else "",
                f"${p.market_value:,.2f}",
                asset_class,
            )
        table.add_section()

    console.print(table)
    console.print()

    # Allocation summary
    alloc_table = Table(title="Portfolio Allocation")
    alloc_table.add_column("Asset Class", style="bold")
    alloc_table.add_column("Value", justify="right", style="green")
    alloc_table.add_column("Percentage", justify="right", style="cyan")

    for cls in sorted(pct_by_class.keys()):
        alloc_table.add_row(
            cls,
            f"${value_by_class[cls]:,.2f}",
            f"{pct_by_class[cls]}%",
        )

    alloc_table.add_section()
    alloc_table.add_row("Total", f"${total_value:,.2f}", "100%", style="bold")

    console.print(alloc_table)


def print_rebalance_report(result: RebalanceResult) -> None:
    """Print the rebalancing report with allocation comparison and trade plan."""
    # Allocation comparison
    alloc_table = Table(title="Allocation Comparison")
    alloc_table.add_column("Asset Class", style="bold")
    alloc_table.add_column("Current %", justify="right")
    alloc_table.add_column("Target %", justify="right")
    alloc_table.add_column("Drift %", justify="right")

    all_classes = sorted(
        set(result.current_allocation.keys()) | set(result.target_allocation.keys())
    )
    for cls in all_classes:
        current = result.current_allocation.get(cls, Decimal("0"))
        target = result.target_allocation.get(cls, Decimal("0"))
        drift = result.drift.get(cls, Decimal("0"))
        drift_style = "red" if drift < 0 else "green" if drift > 0 else ""
        alloc_table.add_row(
            cls,
            f"{current}%",
            f"{target}%",
            f"[{drift_style}]{drift:+}%[/{drift_style}]" if drift_style else f"{drift:+}%",
        )

    console.print()
    console.print(f"[bold]Total Portfolio Value: ${result.total_portfolio_value:,.2f}[/bold]")
    console.print()
    console.print(alloc_table)

    # Trade plan
    if result.trades:
        trade_table = Table(title="Recommended Trades")
        trade_table.add_column("Account", style="cyan")
        trade_table.add_column("Action", style="bold")
        trade_table.add_column("Ticker", style="bold")
        trade_table.add_column("Shares", justify="right")
        trade_table.add_column("Est. Value", justify="right", style="green")
        trade_table.add_column("Reasoning")

        for t in result.trades:
            action_style = "red" if t.action == "SELL" else "green"
            trade_table.add_row(
                t.account_name,
                f"[{action_style}]{t.action}[/{action_style}]",
                t.ticker,
                str(t.shares.quantize(Decimal("0.001"))),
                f"${t.estimated_value:,.2f}",
                t.reasoning,
            )
            for warning in t.warnings:
                trade_table.add_row("", "", "", "", "", f"[yellow]⚠ {warning}[/yellow]")

        console.print()
        console.print(trade_table)
    else:
        console.print("\n[green]Portfolio is within target thresholds. No trades needed.[/green]")

    # Global warnings
    if result.warnings:
        console.print()
        for w in result.warnings:
            console.print(f"[yellow]⚠ {w}[/yellow]")


def write_markdown_report(result: RebalanceResult, path: Path) -> None:
    """Write the rebalance report as a markdown file."""
    lines: list[str] = []
    lines.append("# Portfolio Rebalance Report\n")
    lines.append(f"**Total Portfolio Value:** ${result.total_portfolio_value:,.2f}\n")

    # Allocation table
    lines.append("## Allocation Comparison\n")
    lines.append("| Asset Class | Current % | Target % | Drift % |")
    lines.append("|---|---:|---:|---:|")

    all_classes = sorted(
        set(result.current_allocation.keys()) | set(result.target_allocation.keys())
    )
    for cls in all_classes:
        current = result.current_allocation.get(cls, Decimal("0"))
        target = result.target_allocation.get(cls, Decimal("0"))
        drift = result.drift.get(cls, Decimal("0"))
        lines.append(f"| {cls} | {current}% | {target}% | {drift:+}% |")

    # Trade plan
    if result.trades:
        lines.append("\n## Recommended Trades\n")
        lines.append("| Account | Action | Ticker | Shares | Est. Value | Reasoning |")
        lines.append("|---|---|---|---:|---:|---|")

        for t in result.trades:
            warnings_str = ""
            if t.warnings:
                warnings_str = " " + " ".join(f"⚠ {w}" for w in t.warnings)
            lines.append(
                f"| {t.account_name} | {t.action} | {t.ticker} | "
                f"{t.shares.quantize(Decimal('0.001'))} | ${t.estimated_value:,.2f} | "
                f"{t.reasoning}{warnings_str} |"
            )
    else:
        lines.append("\n**Portfolio is within target thresholds. No trades needed.**\n")

    # Warnings
    if result.warnings:
        lines.append("\n## Warnings\n")
        for w in result.warnings:
            lines.append(f"- ⚠ {w}")

    lines.append("")
    path.write_text("\n".join(lines))
    console.print(f"\n[dim]Report written to {path}[/dim]")
