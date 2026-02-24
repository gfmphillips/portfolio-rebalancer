# Portfolio Rebalancer - User Manual

A tool that analyzes your Fidelity brokerage accounts and tells you exactly what to buy and sell to reach your target allocation. Designed for a Bogle-style three-fund portfolio (VTI/VXUS/BND) but works with any target allocation.

## Table of Contents

1. [Installation](#installation)
2. [Quick Start](#quick-start)
3. [Input: Fidelity CSV](#input-fidelity-csv)
4. [Configuration Files](#configuration-files)
5. [Web UI Guide](#web-ui-guide)
6. [CLI Guide](#cli-guide)
7. [Key Concepts](#key-concepts)
8. [FAQ](#faq)

---

## Installation

Requires Python 3.11+ and [uv](https://docs.astral.sh/uv/).

```bash
git clone <repo-url> portfolio-rebalancer
cd portfolio-rebalancer
uv sync
```

## Quick Start

**Web UI** (recommended):

```bash
uv run streamlit run src/rebalancer/web.py
```

Open http://localhost:8501 in your browser. Upload your Fidelity CSV, review the defaults, and check the tabs.

**CLI**:

```bash
uv run rebalancer run \
  --positions positions.csv \
  --mapping examples/mapping.yaml \
  --config examples/unified_config.yaml
```

## Input: Fidelity CSV

Export your positions from Fidelity:

1. Log into Fidelity.com
2. Go to **Positions** (under "Accounts & Trade")
3. Click **Download** (top-right of positions table)
4. Choose **CSV** format

The file should look like this:

```
Account Name/Number,Symbol,Description,Quantity,Last Price,Current Value,...,Cost Basis Total,...
Individual - TOD...XXX123456,,,,,,,,,,,,
,VTI,VANGUARD TOTAL STOCK MKT ETF,100,"$250.00","$25,000.00",...,"$20,000.00",...
,SPAXX**,FIDELITY GOVERNMENT MONEY MARKET,,,"$2,400.00",...
ROTH IRA...XXX789012,,,,,,,,,,,,
,FXAIX,FIDELITY 500 INDEX FUND,50,"$200.00","$10,000.00",...,"$7,000.00",...
```

The parser handles Fidelity's formatting automatically:
- Dollar signs and commas in numbers (`"$25,000.00"`)
- Trailing `**` on money market symbols (`SPAXX**`)
- Account type detection from account names (Individual = taxable, ROTH IRA = Roth, etc.)
- Blank/summary rows are skipped

## Configuration Files

### mapping.yaml

Maps each ticker to an asset class and defines consolidation targets. You generally don't need to edit this unless you hold tickers not already listed.

```yaml
# End-state funds (your target holdings)
VTI:
  asset_class: us_equity
  similar: [ITOT, SCHB, SPTM]
  preferred: true              # This is a target fund

# Legacy funds (to be consolidated over time)
FXAIX:
  asset_class: us_equity
  similar: [VOO, SPY, IVV]
  consolidate_to: VTI          # Sell FXAIX, buy VTI when tax-efficient

# Cash tickers (no preferred/consolidate_to needed)
SPAXX:
  asset_class: cash
```

**Fields:**

| Field | Required | Description |
|-------|----------|-------------|
| `asset_class` | Yes | One of: `us_equity`, `intl_equity`, `bonds`, `reit`, `cash` |
| `similar` | No | Tickers that are too similar to buy/sell simultaneously (wash sale prevention) |
| `domicile` | No | Fund domicile for German tax PFIC analysis. Default: `US` |
| `preferred` | No | `true` = this is an end-state fund in your target portfolio |
| `consolidate_to` | No | Which preferred ticker this legacy fund should eventually become |

### unified_config.yaml

All settings in a single file. The defaults match a Bogle-style three-fund portfolio:

```yaml
allocation:
  us_equity: 48
  intl_equity: 32
  bonds: 20

rebalance:
  threshold_pct: 5.0            # Absolute drift band: +/-5 percentage points
  threshold_relative_pct: 20.0  # Relative drift band: +/-20% of target
  min_trade_value: 500          # Ignore trades smaller than $500

tax:
  enabled: false                # Tax-aware trading (warns on taxable gains)

accounts:
  "Individual": taxable
  "Taxable": taxable
  "Brokerage": taxable
  "ROTH": roth_ira
  "Rollover": traditional_ira
  "Traditional": traditional_ira
  "401(K)": 401k

german_tax:
  enabled: true
  filing_status: married        # single or married
```

The `accounts` section maps substrings in Fidelity account names to account types. If your account name contains "ROTH", it's classified as a Roth IRA. You only need to edit this if you have unusual account names.

## Web UI Guide

Launch with:

```bash
uv run streamlit run src/rebalancer/web.py
```

### Sidebar (Configuration)

- **Portfolio Data**: Upload your Fidelity CSV or use the example file
- **Target Allocation**: Adjust the percentage for each asset class (must sum to 100)
- **Ticker Mapping**: Edit the mapping YAML if needed (most users won't need to)
- **Rebalance Settings**: Set drift thresholds and minimum trade size
- **Tax**: Enable tax-aware trading to see gain/loss warnings
- **External Cash**: Add bank account cash (see [Cash Buffer](#cash-buffer) below)
- **German Tax**: Toggle German tax annotations (InvStG/Teilfreistellung analysis)
- **Account Types**: Override how account names map to tax treatment
- **Advanced**: View/edit the raw unified config YAML

### Tab 1: Portfolio Overview

Shows your current portfolio:
- Total value, number of accounts, and number of positions
- Pie charts comparing current vs target allocation
- Full positions table with cost basis and gain/loss
- Bank cash holdings (if entered)

### Tab 2: Rebalance Analysis

The decision-making tab:
- **Drift chart**: Bar chart showing how far each class is from target, with threshold lines
- **Allocation table**: Current vs target with both absolute and relative drift
- **Decision checklist**:
  - Which asset classes breach drift bands (and why - absolute, relative, or both)
  - How much idle cash is available
  - How many trades are in retirement vs taxable accounts
  - Warning if taxable trades would realize gains
- **Current vs Target chart**: Side-by-side comparison

### Tab 3: Trade Plan

Step-by-step execution instructions:
- Summary metrics (number of sells/buys, total dollar amounts)
- **Phase 1: Sells** - grouped by account, largest first
- **Phase 2: Buys** - grouped by account, using freed cash
- Running cash balance after each step
- Gain/loss estimates on each trade
- Trade values chart
- German tax annotations (if enabled)
- Markdown report download

### Tab 4: Consolidation

Tracks progress toward your three-fund target:
- **Progress metric**: What % of your non-cash portfolio is in end-state funds (VTI/VXUS/BND) vs legacy funds
- **Progress bar**: Visual indicator
- **Free to Execute**: Legacy positions you can consolidate with no tax cost (retirement accounts or taxable positions at a loss)
- **Wait**: Legacy positions at a gain in taxable accounts (consolidate later when at a loss or when you have a spending need)

## CLI Guide

### Show current allocation

```bash
uv run rebalancer show \
  --positions positions.csv \
  --mapping examples/mapping.yaml
```

### Run rebalance analysis

```bash
uv run rebalancer run \
  --positions positions.csv \
  --mapping examples/mapping.yaml \
  --config examples/unified_config.yaml
```

### Save a markdown report

```bash
uv run rebalancer run \
  --positions positions.csv \
  --mapping examples/mapping.yaml \
  --config examples/unified_config.yaml \
  --output report.md
```

### Enable German tax annotations via CLI flag

```bash
uv run rebalancer run \
  --positions positions.csv \
  --mapping examples/mapping.yaml \
  --config examples/unified_config.yaml \
  --german-tax
```

The CLI output includes:
1. Allocation comparison table (current vs target with drift)
2. Step-by-step trade plan with cash tracking
3. Tax impact summary (if tax-aware trading is on)
4. German tax advisory (if enabled)
5. Consolidation progress report (if legacy positions exist)

## Key Concepts

### Drift Bands

The tool uses dual drift bands to decide whether to rebalance. A class triggers rebalancing if **either** band is breached:

- **Absolute band** (`threshold_pct: 5.0`): Triggers if drift exceeds +/-5 percentage points. Example: target is 48%, current is 54% = 6pp drift, triggers.
- **Relative band** (`threshold_relative_pct: 20.0`): Triggers if drift exceeds +/-20% of the target. Example: target is 20%, current is 15% = 25% relative drift, triggers (even though absolute drift is only 5pp).

The relative band catches drift in smaller allocations that the absolute band would miss. A 5pp drop in a 20% allocation is a 25% relative change - significant enough to act on.

If both bands are within limits, no trades are generated for that class.

### Tax Priority

The tool minimizes tax impact:
1. **Sells in retirement accounts first** (Roth, IRA, 401k) - no tax consequences
2. **Prefers selling losses** in taxable accounts (tax-loss harvesting opportunity)
3. **Warns on taxable gains** so you can make an informed decision
4. **Wash sale detection**: warns if you sell and buy similar tickers within the rebalance

### Cash Pools

Each tax-advantaged account has its own isolated cash pool. You can only buy in a Roth using cash already in that Roth (from existing cash or sell proceeds). Taxable accounts share a single pool - selling in one taxable account frees cash for buying in another.

### Consolidation

Over time, you may accumulate positions across many funds (FXAIX, VGK, VCSH, etc.). The consolidation feature tracks your progress toward a simpler portfolio:

- **Preferred funds** (VTI, VXUS, BND) are your end-state
- **Legacy funds** have a `consolidate_to` target
- The tool tells you which legacy positions are safe to consolidate now (retirement accounts, or taxable positions at a loss) vs which to wait on (taxable positions at a gain)

### Cash Buffer

If your investment policy includes a cash buffer in a bank account (e.g., 90,000 EUR emergency fund), keep it outside the tool. The 48/32/20 allocation applies only to your invested assets. Brokerage idle cash (SPAXX/FDRXX) with a 0% cash target will be flagged for deployment into your three funds.

### German Tax (InvStG)

For investors with German tax obligations, the tool provides:
- **Teilfreistellung** rates per fund category (30% for equity funds, 15% for mixed, 60% for real estate, 0% for bonds)
- **PFIC risk** warnings for non-US-domiciled funds
- **Sparerpauschbetrag** reminder (1,000 EUR single / 2,000 EUR married)

## FAQ

**Q: How often should I rebalance?**
Check quarterly. The dual drift bands prevent unnecessary trading - the tool only suggests trades when your portfolio has meaningfully drifted.

**Q: What if a ticker in my CSV isn't in the mapping?**
It will appear as "unmapped" with a warning. Add it to `mapping.yaml` with the appropriate `asset_class`.

**Q: Can I use this with brokerages other than Fidelity?**
Currently only the Fidelity CSV format is supported. The CSV parser expects Fidelity's specific column headers and formatting.

**Q: Does the tool execute trades?**
No. It only generates recommendations. You execute the trades manually in your brokerage account, following the step-by-step plan.

**Q: What if I don't want German tax annotations?**
Set `german_tax.enabled: false` in `unified_config.yaml`, or toggle it off in the web UI sidebar.

**Q: How do I add a new asset class (e.g., REITs)?**
Add the target percentage in `unified_config.yaml` under `allocation` (making sure everything sums to 100), and ensure the relevant tickers are mapped in `mapping.yaml`.
