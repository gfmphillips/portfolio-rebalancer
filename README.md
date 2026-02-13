# Portfolio Rebalancer

A free, open-source tool that turns your Fidelity positions export into a step-by-step rebalance trade plan.

**[Try it live](https://portfolio-rebalancer-for-americans.streamlit.app)** (no login, no data stored)

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

## Privacy

- **No accounts, no login.** The app runs statelessly in your browser session.
- **No data stored.** Your portfolio data is not saved, transmitted, or accessible to anyone.
- **One external call:** If "Fetch live EUR/USD rate" is enabled, the app requests the current exchange rate from the [Frankfurter API](https://www.frankfurter.app). No portfolio data is included in this request.

## Disclaimer

This tool is for **informational and educational purposes only**. It is not investment advice, financial advice, or tax advice. The developers are not registered investment advisors. You are solely responsible for your own investment decisions. See the full [DISCLAIMER](DISCLAIMER) for details.

Example portfolios and configurations included with this software are for demonstration purposes only and are not investment recommendations.

## Feedback

Using it? Have a feature request? [Share feedback](https://forms.gle/8qMfAWQX9aiWZCo26) (quick Google Form) or [open an issue](https://github.com/gfmphillips/portfolio-rebalancer/issues) on GitHub.

## License

[MIT](LICENSE)
