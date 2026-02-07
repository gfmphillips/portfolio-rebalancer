# Portfolio Rebalancer

## Quick Reference

```bash
# Run tests
uv run pytest

# Run CLI
uv run rebalancer --help
uv run rebalancer show --positions examples/fidelity_positions.csv --mapping examples/mapping.yaml
uv run rebalancer run --positions examples/fidelity_positions.csv --targets examples/targets.yaml --mapping examples/mapping.yaml --config examples/config.yaml
```

## Architecture

- `src/rebalancer/` — main package
  - `cli.py` — Typer CLI entry point
  - `models.py` — Pydantic data models (Position, Trade, RebalanceResult, etc.)
  - `parser.py` — Fidelity CSV parser
  - `config.py` — YAML config loading (targets, mapping, config)
  - `engine.py` — Core rebalancing algorithm
  - `tlh.py` — Tax-loss harvesting + wash sale detection
  - `output.py` — Rich console tables + markdown output

## Key Conventions

- All financial math uses `decimal.Decimal`, never floats
- Account types: taxable, traditional_ira, roth_ira, roth_401k, 401k, hsa
- Tax-advantaged accounts are rebalanced first (free to sell); taxable accounts prefer selling losses
- Fidelity CSV symbols may have trailing `**` (e.g., `SPAXX**`) — strip before lookup
