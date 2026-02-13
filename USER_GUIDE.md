# Portfolio Rebalancer — User Guide

A tool that analyzes your Fidelity brokerage portfolio, compares it to your target allocation, and generates a step-by-step trade plan to get back on track. Available as a web app or command-line tool.

---

## Getting Started (Web UI)

Launch the app:

```bash
uv run streamlit run src/rebalancer/web.py
```

This opens a browser with four tabs and a sidebar of settings. The sidebar is where you configure everything; the tabs show your results.

---

## Step 1: Upload Your Positions

In the sidebar under **Portfolio Data**, upload your Fidelity positions CSV.

**How to get it:** Log into Fidelity → Accounts & Trade → Positions → Download → CSV.

If you just want to explore the tool first, check **Use example CSV** to load a sample portfolio.

## Step 2: Set Your Target Allocation

Under **Target Allocation**, set the percentage you want in each asset class. The values must add up to 100%.

| Asset Class | What It Covers | Example Tickers |
|---|---|---|
| `us_equity` | US stock market | VTI, FXAIX, IVV |
| `intl_equity` | International stocks | VXUS, VGK, VEA |
| `bonds` | Fixed income | BND, VCSH, AGG |
| `reit` | Real estate trusts | VNQ, VNQI |
| `cash` | Brokerage cash | SPAXX, FDRXX |

A common starting point: 48% US equity / 32% international / 20% bonds.

## Step 3: Review the Ticker Mapping

Under **Ticker Mapping**, you'll see a YAML editor that maps each ticker in your portfolio to an asset class. If you have tickers not already listed, add them here. For example:

```yaml
VTI:
  asset_class: us_equity
  preferred: true       # This is your target "end-state" fund

FXAIX:
  asset_class: us_equity
  consolidate_to: VTI   # Legacy fund — consolidate into VTI over time
```

## Step 4: Configure Rebalance Settings

Under **Rebalance Settings**:

- **Absolute threshold %** (default 5): Rebalance if any class drifts more than this many percentage points from target. With 5%, a 48% target triggers at 43% or 53%.
- **Relative threshold %** (default 20): Rebalance if drift exceeds this percentage of the target itself. Catches drift in smaller allocations that the absolute band would miss.
- **Min trade value** (default $500): Ignore trades smaller than this — not worth the effort to execute.

Either threshold being breached triggers a rebalance for that asset class.

## Step 5: Run the Analysis

The tool runs automatically when settings change. Use the tabs to review results:

### Portfolio Overview Tab

Shows your current allocation as pie charts and a positions table. Quick sanity check that the data loaded correctly.

### Rebalance Analysis Tab

The core analysis:

- **Drift chart** — bar chart showing how far each class is from target, with threshold lines
- **Decision checklist** — which classes breach drift bands, idle cash available, trade breakdown
- **Current vs Target** — side-by-side comparison

### Trade Plan Tab

Your step-by-step execution plan:

1. **Phase 1: Sells** — sell overweight positions to free cash (grouped by account)
2. **Phase 2: Buys** — buy underweight positions with the freed cash (grouped by account)

Each step shows the account, ticker, number of shares, dollar value, and reasoning. A running cash balance tracks proceeds as they accumulate.

At the bottom, the **Export** section lets you download:
- **Markdown Report** — formatted text report
- **CSV Report** — spreadsheet-friendly file with summary rows and a trade table (opens in Excel/Google Sheets)

### Consolidation Tab

Tracks your progress from legacy funds (like FXAIX) toward your preferred end-state funds (like VTI). Shows which positions are safe to consolidate now vs. which ones to wait on.

---

## Optional Features

### Tax-Aware Trading

Toggle **Tax-aware trading** in the sidebar to enable:

- Sells in retirement accounts first (no tax impact)
- Prefers selling losses in taxable accounts (tax-loss harvesting)
- Warns on trades that would create taxable gains
- Detects wash sales across accounts

### Tax Lot Data

For more accurate gain/loss estimates, provide your tax lot information. Two options:

1. **Upload a CSV** with columns: `Account, Ticker, AcquisitionDate, Shares, CostBasisPerShare`
2. **Paste from Fidelity** — on the Fidelity Positions page, expand lot details for each position, select all, copy, and paste into the "Paste Fidelity lot data" text area

When lots are provided, the tool uses HIFO (sell highest-cost lots first) in taxable accounts and FIFO in retirement accounts.

### Transaction History

Upload a transaction history CSV to enable **wash sale detection**. If you recently bought a ticker and the tool wants to sell a similar one at a loss, it will flag the conflict.

Format: `Date, Account, Ticker, Action, Shares` — or upload a Fidelity transaction export directly.

### External Cash

Under **External Cash**, enter bank cash that isn't in your brokerage CSV:

- **Investable cash** — available for rebalancing (counted in allocation, can fund buys)
- **Emergency cash** — visible in the portfolio total but excluded from rebalancing

Supports both USD and EUR. The tool fetches a live EUR/USD rate or you can set one manually.

### German Tax Annotations

Toggle **Show German tax annotations** to see InvStG analysis on each trade: Teilfreistellung rates, PFIC risk warnings for non-US funds, and Sparerpauschbetrag reminders.

---

## Command-Line Usage

For scripting or automation, the CLI offers the same features:

```bash
# View current allocation
uv run rebalancer show \
  --positions positions.csv \
  --mapping mapping.yaml

# Generate trade plan
uv run rebalancer run \
  --positions positions.csv \
  --mapping mapping.yaml \
  --config config.yaml \
  --output report.md

# With all optional features
uv run rebalancer run \
  --positions positions.csv \
  --mapping mapping.yaml \
  --config config.yaml \
  --transactions transactions.csv \
  --lots tax_lots.csv \
  --output report.md \
  --german-tax
```

The config file can be a single unified YAML with all settings (allocation, rebalance thresholds, tax, accounts, cash, output). See `examples/` for templates.

---

## How It Decides What to Trade

1. **Compute drift** — for each asset class, compare current % to target %
2. **Check thresholds** — if absolute OR relative drift exceeds the band, that class needs rebalancing
3. **Generate sells** — sell overweight positions, starting with retirement accounts (tax-free), then taxable accounts (preferring losses)
4. **Generate buys** — buy underweight positions using freed cash
5. **Respect cash pools** — taxable accounts share one cash pool; each retirement account has its own isolated pool (no transfers between IRAs)

---

## Tips

- **Start with the example CSV** to understand the tool before uploading real data
- **Set cash target to 0%** if you want to deploy all idle brokerage cash into funds
- **Check the Consolidation tab** periodically to track progress toward your target fund lineup
- **The tool never executes trades** — it only generates a plan. You execute manually in your brokerage
- **All math uses exact decimal arithmetic** — no floating-point rounding errors
