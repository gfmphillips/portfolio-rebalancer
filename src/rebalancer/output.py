from decimal import Decimal
from pathlib import Path

from rich.console import Console
from rich.table import Table

from collections import OrderedDict

from .models import OutputConfig, Position, RebalanceResult, SortKey, TickerMapping, Trade


# ---------------------------------------------------------------------------
# Execution plan helpers
# ---------------------------------------------------------------------------

class ExecutionStep:
    """A single numbered step in the execution plan."""

    def __init__(
        self,
        step_num: int,
        phase: str,
        trade: Trade,
        cash_after: Decimal,
        note: str = "",
    ):
        self.step_num = step_num
        self.phase = phase  # "SELL" or "BUY"
        self.trade = trade
        self.cash_after = cash_after
        self.note = note


def build_execution_plan(trades: list[Trade]) -> list[ExecutionStep]:
    """Organize trades into a numbered execution plan.

    Order: all sells first (grouped by account, largest first within account),
    then all buys (grouped by account, largest first within account).
    Tracks a running cash tally so the user can see proceeds accumulating
    before buys spend them.
    """
    sells = [t for t in trades if t.action == "SELL"]
    buys = [t for t in trades if t.action == "BUY"]

    def _group_by_account(trade_list: list[Trade]) -> OrderedDict[str, list[Trade]]:
        groups: OrderedDict[str, list[Trade]] = OrderedDict()
        for t in sorted(trade_list, key=lambda t: t.account_name):
            groups.setdefault(t.account_name, []).append(t)
        # Sort within each account: largest value first
        for acct in groups:
            groups[acct] = sorted(groups[acct], key=lambda t: t.estimated_value, reverse=True)
        return groups

    sell_groups = _group_by_account(sells)
    buy_groups = _group_by_account(buys)

    steps: list[ExecutionStep] = []
    cash = Decimal("0")
    step_num = 0

    for acct, acct_trades in sell_groups.items():
        for t in acct_trades:
            step_num += 1
            cash += t.estimated_value
            note = ""
            if t.warnings:
                note = "; ".join(t.warnings)
            steps.append(ExecutionStep(
                step_num=step_num, phase="SELL", trade=t,
                cash_after=cash, note=note,
            ))

    for acct, acct_trades in buy_groups.items():
        for t in acct_trades:
            step_num += 1
            cash -= t.estimated_value
            steps.append(ExecutionStep(
                step_num=step_num, phase="BUY", trade=t,
                cash_after=cash, note="",
            ))

    return steps

console = Console()


def _format_currency(val: Decimal, precision: int = 0) -> str:
    """Format a Decimal as a currency string with configurable precision."""
    if precision == 0:
        return f"${float(val):,.0f}"
    return f"${float(val):,.{precision}f}"


def _format_pct(val: Decimal, precision: int = 2) -> str:
    """Format a Decimal as a percentage string with configurable precision."""
    return f"{float(val):.{precision}f}%"


def sort_trades(trades: list[Trade], sort_order: list[SortKey]) -> list[Trade]:
    """Sort trades according to a list of sort keys (applied in reverse priority)."""
    result = list(trades)
    for key in reversed(sort_order):
        if key == SortKey.SELLS_FIRST:
            result.sort(key=lambda t: (0 if t.action == "SELL" else 1))
        elif key == SortKey.BUYS_FIRST:
            result.sort(key=lambda t: (0 if t.action == "BUY" else 1))
        elif key == SortKey.LARGEST_TRADE_FIRST:
            result.sort(key=lambda t: t.estimated_value, reverse=True)
        elif key == SortKey.SMALLEST_TRADE_FIRST:
            result.sort(key=lambda t: t.estimated_value)
        elif key == SortKey.BY_ACCOUNT:
            result.sort(key=lambda t: t.account_name)
        elif key == SortKey.BY_TICKER:
            result.sort(key=lambda t: t.ticker)
    return result


def filter_actionable_trades(
    trades: list[Trade], min_value: Decimal, show_only: bool
) -> list[Trade]:
    """Filter trades to only those above min_value when show_only is True."""
    if not show_only:
        return list(trades)
    return [t for t in trades if t.estimated_value >= min_value]


def _compute_allocation(
    positions: list[Position],
    mapping: dict[str, TickerMapping],
    pct_precision: int = 2,
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
        quantizer = Decimal("0.1") ** pct_precision if pct_precision > 0 else Decimal("1")
        for cls, val in value_by_class.items():
            pct_by_class[cls] = (val / total_value * Decimal("100")).quantize(quantizer)

    return total_value, value_by_class, pct_by_class


def print_allocation_table(
    positions: list[Position],
    mapping: dict[str, TickerMapping],
    output_config: OutputConfig | None = None,
) -> None:
    """Print a rich table showing current portfolio allocation."""
    oc = output_config or OutputConfig()
    total_value, value_by_class, pct_by_class = _compute_allocation(
        positions, mapping, pct_precision=oc.precision.pct
    )

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


def print_rebalance_report(
    result: RebalanceResult, output_config: OutputConfig | None = None
) -> None:
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
        steps = build_execution_plan(result.trades)
        sells = [s for s in steps if s.phase == "SELL"]
        buys = [s for s in steps if s.phase == "BUY"]
        total_sell = sum(s.trade.estimated_value for s in sells)
        total_buy = sum(s.trade.estimated_value for s in buys)
        sell_accounts = len({s.trade.account_name for s in sells})
        buy_accounts = len({s.trade.account_name for s in buys})

        console.print()
        console.print("[bold]Execution Plan[/bold]")
        console.print(
            f"  {len(sells)} sell(s) across {sell_accounts} account(s)  "
            f"[dim]→ frees[/dim] [green]${total_sell:,.2f}[/green]"
        )
        console.print(
            f"  {len(buys)} buy(s) across {buy_accounts} account(s)   "
            f"[dim]→ costs[/dim] [red]${total_buy:,.2f}[/red]"
        )
        console.print()
        console.print("[dim]Execute all sells first, then buys. Work through one account at a time.[/dim]")

        trade_table = Table(title="Step-by-Step Trade Plan")
        trade_table.add_column("Step", justify="right", style="bold")
        trade_table.add_column("Account", style="cyan")
        trade_table.add_column("Action", style="bold")
        trade_table.add_column("Ticker", style="bold")
        trade_table.add_column("Shares", justify="right")
        trade_table.add_column("Est. Value", justify="right", style="green")
        trade_table.add_column("Cash After", justify="right", style="dim")
        trade_table.add_column("Reasoning")

        prev_phase = None
        prev_account = None
        for s in steps:
            if s.phase != prev_phase:
                if prev_phase is not None:
                    trade_table.add_section()
                prev_account = None
                prev_phase = s.phase

            if s.trade.account_name != prev_account:
                if prev_account is not None:
                    trade_table.add_section()
                prev_account = s.trade.account_name

            t = s.trade
            action_style = "red" if t.action == "SELL" else "green"
            trade_table.add_row(
                str(s.step_num),
                t.account_name,
                f"[{action_style}]{t.action}[/{action_style}]",
                t.ticker,
                str(t.shares.quantize(Decimal("0.001"))),
                f"${t.estimated_value:,.2f}",
                f"${s.cash_after:,.2f}",
                t.reasoning,
            )
            for warning in t.warnings:
                trade_table.add_row("", "", "", "", "", "", "", f"[yellow]⚠ {warning}[/yellow]")

        console.print()
        console.print(trade_table)
    else:
        console.print("\n[green]Portfolio is within target thresholds. No trades needed.[/green]")

    # Tax impact summary
    ti = result.tax_impact
    if ti.taxable_trades_count > 0:
        console.print()
        console.print("[bold]Estimated Tax Impact (Taxable Accounts Only)[/bold]")
        if ti.estimated_total_gains > 0:
            console.print(f"  Estimated gains:  [red]${ti.estimated_total_gains:,.2f}[/red]")
        if ti.estimated_total_losses < 0:
            console.print(f"  Estimated losses: [green]${ti.estimated_total_losses:,.2f}[/green]")
        net_style = "red" if ti.estimated_net > 0 else "green"
        console.print(f"  Net:              [{net_style}]${ti.estimated_net:,.2f}[/{net_style}]")

    # Global warnings
    if result.warnings:
        console.print()
        for w in result.warnings:
            console.print(f"[yellow]⚠ {w}[/yellow]")


def write_markdown_report(
    result: RebalanceResult, path: Path, output_config: OutputConfig | None = None
) -> None:
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
        steps = build_execution_plan(result.trades)
        sells = [s for s in steps if s.phase == "SELL"]
        buys = [s for s in steps if s.phase == "BUY"]
        total_sell = sum(s.trade.estimated_value for s in sells)
        total_buy = sum(s.trade.estimated_value for s in buys)

        lines.append("\n## Execution Plan\n")
        lines.append(
            f"**{len(sells)} sell(s)** frees ${total_sell:,.2f} | "
            f"**{len(buys)} buy(s)** costs ${total_buy:,.2f}\n"
        )
        lines.append("> Execute all sells first, then buys. Work through one account at a time.\n")

        lines.append("| Step | Account | Action | Ticker | Shares | Est. Value | Cash After | Reasoning |")
        lines.append("|---:|---|---|---|---:|---:|---:|---|")

        for s in steps:
            t = s.trade
            warnings_str = ""
            if t.warnings:
                warnings_str = " " + " ".join(f"⚠ {w}" for w in t.warnings)
            lines.append(
                f"| {s.step_num} | {t.account_name} | {t.action} | {t.ticker} | "
                f"{t.shares.quantize(Decimal('0.001'))} | ${t.estimated_value:,.2f} | "
                f"${s.cash_after:,.2f} | {t.reasoning}{warnings_str} |"
            )
    else:
        lines.append("\n**Portfolio is within target thresholds. No trades needed.**\n")

    # Tax impact
    ti = result.tax_impact
    if ti.taxable_trades_count > 0:
        lines.append("\n## Estimated Tax Impact (Taxable Accounts Only)\n")
        if ti.estimated_total_gains > 0:
            lines.append(f"- Estimated gains: ${ti.estimated_total_gains:,.2f}")
        if ti.estimated_total_losses < 0:
            lines.append(f"- Estimated losses: ${ti.estimated_total_losses:,.2f}")
        lines.append(f"- **Net: ${ti.estimated_net:,.2f}**")

    # Warnings
    if result.warnings:
        lines.append("\n## Warnings\n")
        for w in result.warnings:
            lines.append(f"- ⚠ {w}")

    lines.append("")
    path.write_text("\n".join(lines))
    console.print(f"\n[dim]Report written to {path}[/dim]")
