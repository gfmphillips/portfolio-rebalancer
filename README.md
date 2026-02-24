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

## Policy-aware mode (current default UI)

The app operates under a strict no-new-ETF policy:

- **New purchases** are limited to individual stocks (via a stock basket CSV) and Treasury/CD placeholders
- **Legacy ETFs/funds** can be held and sold but never bought again
- **IRA/Roth accounts** are buy-frozen (can sell, cannot buy new positions)
- **Band detection** uses the total portfolio view; routing/execution uses only buy-enabled accounts

### Targets & configuration

Set in the **Settings** tab or edit `~/.portfolio-rebalancer/settings.json`:

| Setting | Default | Description |
|---|---|---|
| `target_stock_pct` | 80% | Strategic stock allocation |
| `target_bond_pct` | 20% | Strategic defensive allocation |
| `rebalance_band_abs` | ±5 pp | Outside-band threshold |
| `horizon_months` | 18 | Flag if new-money correction takes longer |
| `investable_cash_eur` | 0 | One-time cash to deploy this cycle |
| `monthly_investable_cash_eur` | 0 | Monthly savings rate (for horizon estimate) |
| `defensive_mode` | `treasury_only` | `treasury_only` / `treasury_cd_split` / `ladder` |
| `buy_enabled_account_types` | taxable | Account types that can receive new buys |
| `allow_legacy_etf_sales` | false | Enable sell recommendations for legacy ETFs |

### Stock basket

Upload a CSV to the **Settings → Basket** section (or download the template):

```
ticker,target_weight,name,sector,country,is_adr
AAPL,7.00,Apple Inc.,Technology,US,false
MSFT,5.50,Microsoft Corp.,Technology,US,false
```

- Weights can be 0–100 (percentages) or 0–1 (fractions); auto-detected and normalized
- Filename `basket_us_equity_vYYYY-MM-DD.csv` sets the basket version badge
- Top-up math: `target = w_i × (existing_basket_value + new_equity_cash)` — existing holdings are credited, so only the gap is purchased

### Limitations

- No SELL recommendations for individual stocks (sell side not yet implemented)
- Basket orders assume whole shares only; fractional shares not supported
- Defensive placeholders (TREASURY, CD) require manual execution at your broker
- `allow_legacy_etf_sales=True` is irreversible — ETFs sold cannot be repurchased

---

## How it works (legacy engine)

The original rebalance engine (`rebalance()` in `engine.py`) remains intact and is available via CLI:

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
