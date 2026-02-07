# Portfolio Rebalancer

A local-first CLI tool to rebalance Fidelity investment accounts. Import Fidelity position CSVs, compare current allocations against target asset-class allocations, and get an actionable trade plan.

## Setup

```bash
uv sync
```

## Usage

### Show current allocation

```bash
uv run rebalancer show \
  --positions examples/fidelity_positions.csv \
  --mapping examples/mapping.yaml
```

### Run rebalancing

```bash
uv run rebalancer run \
  --positions examples/fidelity_positions.csv \
  --targets examples/targets.yaml \
  --mapping examples/mapping.yaml \
  --config examples/config.yaml
```

Optionally write a markdown report:

```bash
uv run rebalancer run \
  --positions examples/fidelity_positions.csv \
  --targets examples/targets.yaml \
  --mapping examples/mapping.yaml \
  --config examples/config.yaml \
  --output report.md
```

## Configuration

- **targets.yaml** — Target asset class allocation percentages (must sum to 100)
- **mapping.yaml** — Map tickers to asset classes and define similar tickers for wash sale detection
- **config.yaml** — Rebalancing thresholds, tax settings, and account type mappings

## Testing

```bash
uv run pytest
```
