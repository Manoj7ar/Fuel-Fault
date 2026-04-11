# Fuel Fault Lines

Irish county-level **energy vulnerability** dashboard and API: SEAI-style energy profiles, CSO deprivation (or a synthetic fallback), and a liquid-fuel price scenario model. Built for exploration in **Zerve** and deployable as a **FastAPI hub** (see `api/main.py`).

## Quick start

**Backend**

```bash
cd api && pip install -r requirements.txt && uvicorn main:app --host 0.0.0.0 --port 8000
```

**Frontend** (optional local proxy; zero npm deps)

```bash
node dev-server.mjs
```

Open `http://127.0.0.1:5500/`. The dev server proxies `/api/*` to the deployed hub by default; for local FastAPI, set `window.__FFL_API_BASE__ = 'http://127.0.0.1:8000'` in the browser console before load, or use the API directly.

## API highlights

| Route | Purpose |
|--------|---------|
| `GET /health` | Status + `api_version`, `git_rev`, `built_at_utc` |
| `GET /meta` | Lineage, limitations, `params`, Zerve hints, **demo script for judges**, endpoint index |
| `GET /counties` | County name list (for bootstrapping the UI) |
| `GET /county/{county}` | County snapshot at `fuel_price` |
| `GET /national/snapshot` | Headline national stats at `price_eur_l` |
| `GET /insights/narrative` | Auto bullets + elevator pitch for video / Devpost |
| `GET /insights/regional` | Province roll-ups at `fuel_price` |
| `GET /model/scenario-curve` | Counties over threshold vs €/L (`price_min`, `price_max`, `steps`) |
| `GET /model/ranking-stability` | Top‑k overlap when tilting composite weights |
| `GET /compare/counties` | Side-by-side two counties + narrative delta |
| `GET /export/county/{county}` | Markdown brief (journalists / briefings) |
| `GET /export/briefing` | **National** one-page markdown (claims summary + validation + lineage) |
| `GET /model/claims` | Pass/fail checks (incl. correlations + breach-price spread) |
| `GET /model/validation` | Internal correlation sanity checks |
| `GET /model/distribution` | Deciles of fuel-income share across counties |
| `GET /model/breach-prices` | Per-county €/L where stress crosses threshold (“fault line”) |
| `GET /model/sensitivity` | Stress tests (litres, weights, HDD) |
| `GET /model/policy` | Illustrative universal vs targeted grant outlay |
| `POST /model/params` | Session-local assumption tuning (rebuilds model unless `USE_ZERVE_VARIABLE`) |
| `GET /docs` | **OpenAPI** (tagged: core, county, scenario, model, insights, export) |
| `GET /scenario`, `/history`, `/deep-dive/{county}` | Existing dashboard routes |

## Zerve workflow

1. Explore and build the dataframe in a Zerve notebook (e.g. block `warmer_homes_roi`, variable `warmer_homes_df`).
2. Deploy this repo’s `api/` as a FastAPI app on Zerve.
3. Optional: set `USE_ZERVE_VARIABLE=1` and matching `ZERVE_DATA_BLOCK` / `ZERVE_DATA_VAR` so the hub reads the notebook output.

## Judging / narrative hooks

- **Method & lab** page surfaces `/meta`, judge demo script, national snapshot, validation, distribution, breach prices, regional summary, claims, sensitivity, policy, parameter lab, and national briefing export.
- **Heating-demand (HDD)** multipliers scale the litres proxy by county (documented in code).
- **Limitations** are returned in `/meta` and repeated in export briefs: synthetic income bands, proxy fuel use, not administrative fuel poverty counts.
