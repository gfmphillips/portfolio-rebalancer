import csv
import io
from collections import OrderedDict, defaultdict
from datetime import date, datetime
from decimal import Decimal
from pathlib import Path

from rich.console import Console
from rich.table import Table

from .models import (
    HUNDRED,
    ZERO,
    ConsolidationAnalysis,
    ConstraintCheck,
    GermanTaxAnnotation,
    GermanTaxConfig,
    OutputConfig,
    Position,
    RebalanceResult,
    SortKey,
    TickerMapping,
    Trade,
)


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
    cash = ZERO
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
            asset_class, ZERO
        ) + p.market_value

    pct_by_class: dict[str, Decimal] = {}
    if total_value > 0:
        quantizer = Decimal("0.1") ** pct_precision if pct_precision > 0 else Decimal("1")
        for cls, val in value_by_class.items():
            pct_by_class[cls] = (val / total_value * HUNDRED).quantize(quantizer)

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
    accounts: defaultdict[str, list[Position]] = defaultdict(list)
    for p in positions:
        accounts[p.account_name].append(p)

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
        current = result.current_allocation.get(cls, ZERO)
        target = result.target_allocation.get(cls, ZERO)
        drift = result.drift.get(cls, ZERO)
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
        has_lot_trades = any(t.lot_acquisition_date is not None for t in result.trades if t.action == "SELL")
        has_blended_trades = any(t.lot_acquisition_date is None for t in result.trades if t.action == "SELL")
        if has_lot_trades and not has_blended_trades:
            basis_label = " (lot-specific)"
        elif has_lot_trades and has_blended_trades:
            basis_label = " (mixed: lot-specific + approximate)"
        else:
            basis_label = " (approximate)"
        console.print()
        console.print(f"[bold]Estimated Tax Impact (Taxable Accounts Only){basis_label}[/bold]")
        if ti.estimated_total_gains > 0:
            console.print(f"  Estimated gains:  [red]${ti.estimated_total_gains:,.2f}[/red]")
        if ti.estimated_total_losses < 0:
            console.print(f"  Estimated losses: [green]${ti.estimated_total_losses:,.2f}[/green]")
        net_style = "red" if ti.estimated_net > 0 else "green"
        console.print(f"  Net:              [{net_style}]${ti.estimated_net:,.2f}[/{net_style}]")

    # Constraint checks
    if result.constraints:
        console.print()
        console.print("[bold]Constraint Checks[/bold]")
        ct = Table()
        ct.add_column("Constraint", style="bold")
        ct.add_column("Required", justify="right")
        ct.add_column("Actual", justify="right")
        ct.add_column("Status", justify="center")
        for cc in result.constraints:
            status = "[green]MET[/green]" if cc.met else "[red]VIOLATED[/red]"
            ct.add_row(cc.name, f"${cc.required:,}", f"${cc.actual:,}", status)
        console.print(ct)

    # Global warnings
    if result.warnings:
        console.print()
        for w in result.warnings:
            console.print(f"[yellow]⚠ {w}[/yellow]")

    # Run metadata footer
    if result.metadata:
        console.print()
        m = result.metadata
        console.print(f"[dim]Run: {m.timestamp} | EUR/USD: {m.eurusd_fx_used} | v{m.tool_version}[/dim]")


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
        current = result.current_allocation.get(cls, ZERO)
        target = result.target_allocation.get(cls, ZERO)
        drift = result.drift.get(cls, ZERO)
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
        has_lot_trades = any(t.lot_acquisition_date is not None for t in result.trades if t.action == "SELL")
        has_blended_trades = any(t.lot_acquisition_date is None for t in result.trades if t.action == "SELL")
        if has_lot_trades and not has_blended_trades:
            basis_label = " (lot-specific)"
        elif has_lot_trades and has_blended_trades:
            basis_label = " (mixed: lot-specific + approximate)"
        else:
            basis_label = " (approximate)"
        lines.append(f"\n## Estimated Tax Impact (Taxable Accounts Only){basis_label}\n")
        if ti.estimated_total_gains > 0:
            lines.append(f"- Estimated gains: ${ti.estimated_total_gains:,.2f}")
        if ti.estimated_total_losses < 0:
            lines.append(f"- Estimated losses: ${ti.estimated_total_losses:,.2f}")
        lines.append(f"- **Net: ${ti.estimated_net:,.2f}**")

    # Constraints
    if result.constraints:
        lines.append("\n## Constraint Checks\n")
        lines.append("| Constraint | Required | Actual | Status |")
        lines.append("|---|---:|---:|:---:|")
        for cc in result.constraints:
            status = "MET" if cc.met else "VIOLATED"
            lines.append(f"| {cc.name} | ${cc.required:,} | ${cc.actual:,} | {status} |")

    # Warnings
    if result.warnings:
        lines.append("\n## Warnings\n")
        for w in result.warnings:
            lines.append(f"- ⚠ {w}")

    # Run metadata
    if result.metadata:
        m = result.metadata
        lines.append(f"\n---\n*Run: {m.timestamp} | EUR/USD: {m.eurusd_fx_used} | v{m.tool_version}*")

    lines.append("")
    path.write_text("\n".join(lines))
    console.print(f"\n[dim]Report written to {path}[/dim]")


def _compute_term(lot_date_str: str | None) -> str:
    """Derive 'Short-term' or 'Long-term' from a lot acquisition date string."""
    if not lot_date_str:
        return ""
    try:
        acq = datetime.strptime(lot_date_str, "%Y-%m-%d").date()
    except ValueError:
        return ""
    days_held = (date.today() - acq).days
    return "Short-term" if days_held < 365 else "Long-term"


def write_csv_report(
    result: RebalanceResult, path: Path, output_config: OutputConfig | None = None
) -> None:
    """Write the rebalance report as a CSV file.

    The CSV contains summary header rows (prefixed with ``#``) followed by a
    trade table with one row per execution step.
    """
    buf = io.StringIO()
    writer = csv.writer(buf)

    # --- Summary header rows ---
    writer.writerow(["# Portfolio Total", f"${result.total_portfolio_value:,.2f}"])
    writer.writerow(["# Date", date.today().isoformat()])

    ti = result.tax_impact
    writer.writerow([
        "# Tax Impact",
        f"Gains: ${ti.estimated_total_gains:,.2f}",
        f"Losses: ${ti.estimated_total_losses:,.2f}",
        f"Net: ${ti.estimated_net:,.2f}",
    ])

    all_classes = sorted(
        set(result.current_allocation.keys()) | set(result.target_allocation.keys())
    )
    drift_parts = []
    for cls in all_classes:
        current = result.current_allocation.get(cls, ZERO)
        target = result.target_allocation.get(cls, ZERO)
        drift = result.drift.get(cls, ZERO)
        drift_parts.append(f"{cls}: {current}% -> {target}% ({drift:+}%)")
    writer.writerow(["# Allocation Drift"] + drift_parts)

    # Empty separator row
    writer.writerow([])

    # --- Trade table ---
    columns = [
        "Step", "Phase", "Account", "Account Type", "Ticker", "Action",
        "Shares", "Est. Value", "Est. Gain/Loss", "Lot Date", "Term",
        "Reasoning", "Warnings",
    ]
    writer.writerow(columns)

    if result.trades:
        steps = build_execution_plan(result.trades)
        for s in steps:
            t = s.trade
            writer.writerow([
                s.step_num,
                s.phase,
                t.account_name,
                t.account_type.value,
                t.ticker,
                t.action,
                str(t.shares.quantize(Decimal("0.001"))),
                f"${t.estimated_value:,.2f}",
                f"${t.estimated_gain_loss:,.2f}" if t.estimated_gain_loss is not None else "",
                t.lot_acquisition_date or "",
                _compute_term(t.lot_acquisition_date),
                t.reasoning,
                "; ".join(t.warnings) if t.warnings else "",
            ])

    path.write_text(buf.getvalue())


def print_german_tax_section(
    annotations: list[GermanTaxAnnotation],
    config: GermanTaxConfig,
) -> None:
    """Print German tax advisory annotations as a Rich table."""
    if not annotations:
        console.print("\n[dim]No taxable trades -- German tax annotations not applicable.[/dim]")
        return

    from .german_tax import generate_summary

    console.print()
    console.print("[bold]German Tax Advisory (InvStG)[/bold]")

    table = Table(title="Teilfreistellung & PFIC Analysis")
    table.add_column("Ticker", style="bold")
    table.add_column("Category")
    table.add_column("Teilfreistellung", justify="right")
    table.add_column("PFIC Risk", justify="center")
    table.add_column("Domicile", justify="center")
    table.add_column("Notes")

    for a in annotations:
        pfic_str = "[red]YES[/red]" if a.pfic_risk else "[green]No[/green]"
        table.add_row(
            a.ticker,
            a.fund_category.value,
            f"{a.teilfreistellung_pct}%",
            pfic_str,
            a.domicile,
            "; ".join(a.notes),
        )

    console.print(table)

    summary = generate_summary(annotations, config.filing_status)
    sparer = summary["sparerpauschbetrag_eur"]
    console.print(
        f"\n[dim]Sparerpauschbetrag: EUR {sparer:,} "
        f"({config.filing_status}). "
        f"First EUR {sparer:,} of investment income is tax-free.[/dim]"
    )
    if summary["pfic_risk_count"] > 0:
        console.print(
            f"[yellow]Warning: {summary['pfic_risk_count']} position(s) "
            f"with PFIC risk. Consult your tax advisor.[/yellow]"
        )


def write_german_tax_markdown(
    annotations: list[GermanTaxAnnotation],
    config: GermanTaxConfig,
) -> list[str]:
    """Return markdown lines for the German tax advisory section."""
    lines: list[str] = []

    if not annotations:
        lines.append("\n## German Tax Advisory\n")
        lines.append("No taxable trades -- German tax annotations not applicable.\n")
        return lines

    from .german_tax import generate_summary

    lines.append("\n## German Tax Advisory (InvStG)\n")
    lines.append("| Ticker | Category | Teilfreistellung | PFIC Risk | Domicile | Notes |")
    lines.append("|---|---|---:|:---:|:---:|---|")

    for a in annotations:
        pfic_str = "YES" if a.pfic_risk else "No"
        notes_str = "; ".join(a.notes)
        lines.append(
            f"| {a.ticker} | {a.fund_category.value} | {a.teilfreistellung_pct}% "
            f"| {pfic_str} | {a.domicile} | {notes_str} |"
        )

    summary = generate_summary(annotations, config.filing_status)
    sparer = summary["sparerpauschbetrag_eur"]
    lines.append(
        f"\n**Sparerpauschbetrag:** EUR {sparer:,} ({config.filing_status}). "
        f"First EUR {sparer:,} of investment income is tax-free."
    )
    if summary["pfic_risk_count"] > 0:
        lines.append(
            f"\n**Warning:** {summary['pfic_risk_count']} position(s) with PFIC risk. "
            "Consult your tax advisor."
        )

    return lines


def print_consolidation_report(analysis: ConsolidationAnalysis) -> None:
    """Print a Rich report showing consolidation progress and opportunities."""
    console.print()
    console.print("[bold]Consolidation Progress[/bold]")
    console.print(
        f"  End-state funds: [green]{_format_currency(analysis.end_state_value)}[/green] "
        f"({analysis.end_state_pct}%)"
    )
    console.print(
        f"  Legacy funds:    [yellow]{_format_currency(analysis.legacy_value)}[/yellow] "
        f"({analysis.legacy_pct}%)"
    )

    if not analysis.opportunities:
        console.print("\n[green]No consolidation opportunities (all positions are end-state).[/green]")
        return

    table = Table(title="Consolidation Opportunities")
    table.add_column("Ticker", style="bold")
    table.add_column("Account", style="cyan")
    table.add_column("Value", justify="right", style="green")
    table.add_column("Consolidate To", style="bold")
    table.add_column("Safe?", justify="center")
    table.add_column("Gain/Loss", justify="right")
    table.add_column("Reason")

    for opp in analysis.opportunities:
        safe_str = "[green]Yes[/green]" if opp.safe_to_consolidate else "[yellow]Wait[/yellow]"
        gl_str = _format_currency(opp.estimated_gain_loss) if opp.estimated_gain_loss is not None else "-"
        table.add_row(
            opp.ticker,
            opp.account_name,
            _format_currency(opp.market_value),
            opp.consolidate_to,
            safe_str,
            gl_str,
            opp.reason,
        )

    console.print()
    console.print(table)


def write_consolidation_markdown(analysis: ConsolidationAnalysis) -> list[str]:
    """Return markdown lines for the consolidation report."""
    lines: list[str] = []
    lines.append("\n## Consolidation Progress\n")
    lines.append(
        f"- **End-state funds:** {_format_currency(analysis.end_state_value)} "
        f"({analysis.end_state_pct}%)"
    )
    lines.append(
        f"- **Legacy funds:** {_format_currency(analysis.legacy_value)} "
        f"({analysis.legacy_pct}%)"
    )

    if not analysis.opportunities:
        lines.append("\nNo consolidation opportunities (all positions are end-state).\n")
        return lines

    lines.append("\n| Ticker | Account | Value | Consolidate To | Safe? | Gain/Loss | Reason |")
    lines.append("|---|---|---:|---|:---:|---:|---|")

    for opp in analysis.opportunities:
        safe_str = "Yes" if opp.safe_to_consolidate else "Wait"
        gl_str = _format_currency(opp.estimated_gain_loss) if opp.estimated_gain_loss is not None else "-"
        lines.append(
            f"| {opp.ticker} | {opp.account_name} | {_format_currency(opp.market_value)} "
            f"| {opp.consolidate_to} | {safe_str} | {gl_str} | {opp.reason} |"
        )

    return lines
