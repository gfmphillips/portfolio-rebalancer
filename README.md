# Portfolio Rebalancer

A free, open-source tool that turns your Fidelity positions export into a step-by-step rebalance trade plan.

**[Try it live](https://portfolio-rebalancer.streamlit.app)** (no login, no data stored)

<!-- TODO: Add screenshot once deployed -->
<!-- ![Screenshot](docs/screenshot.png) -->

## What it does

- Upload a Fidelity positions CSV (or use the built-in example)
- Set your target asset class allocation (e.g. 48% US equity, 32% international, 20% bonds)
- Get an actionable trade plan: what to sell, what to buy, in what order
- Tax-aware: sells in retirement accounts first, harvests losses in taxable accounts
- Tracks three-fund consolidation progress from legacy holdings
- German tax annotations (InvStG) for expats

## Quick start (local)

Requires Python 3.11+ and [uv](https://docs.astral.sh/uv/).

```bash
git clone https://github.com/gfmphillips/portfolio-rebalancer.git
cd portfolio-rebalancer
uv sync
uv run streamlit run src/rebalancer/web.py
```

The app opens at `http://localhost:8501` with example data pre-loaded.

### CLI

```bash
uv run rebalancer run \
  --positions examples/fidelity_positions.csv \
  --targets examples/targets.yaml \
  --mapping examples/mapping.yaml \
  --config examples/config.yaml
```

## How it works

1. **Parse** your Fidelity CSV export (handles dollar signs, trailing `**` on money market symbols, account type detection)
2. **Compare** current allocation against your targets using absolute and relative drift bands
3. **Generate trades** that minimize tax impact: retirement accounts first, loss-harvesting in taxable
4. **Output** a step-by-step execution plan with cash pool tracking across accounts

Your data never leaves your browser (on Streamlit Cloud) or your machine (local). Nothing is stored.

## Feedback

Using it? Have a feature request? [Open an issue](https://github.com/gfmphillips/portfolio-rebalancer/issues) or [start a discussion](https://github.com/gfmphillips/portfolio-rebalancer/discussions).

## License

[MIT](LICENSE)
