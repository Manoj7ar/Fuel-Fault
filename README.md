# Fuel Fault Lines

**Fuel Fault Lines** is a single-page dashboard and optional **FastAPI** backend for exploring **Irish county-level energy vulnerability**: SEAI-style energy profiles, CSO deprivation signals (with a synthetic fallback when needed), and a **liquid-fuel price scenario** model. The UI is built for **Zerve** notebooks and a deployable **Zerve FastAPI hub**; the same ideas are implemented locally in `api/`.

There is **no database**. Data is computed in memory from public sources (SEAI, CSO) with **hardcoded fallbacks** when live fetches fail.

---

## What’s in this repo

| Piece | Role |
|--------|------|
| **`index.html`** | Full app: dashboard, scenarios, compare matrices, counties register, method & lab, demo & submit, optional **AI · Gemini** chat. Vanilla HTML/CSS/JS; **no build step**. |
| **`dev-server.mjs`** | Static file server (port **5500**) plus a **CORS-safe HTTPS proxy**: browser calls `http://127.0.0.1:5500/api/...` → `https://<hub>/...`. **Zero npm dependencies** (Node built-ins only). |
| **`api/`** | **FastAPI** app: data pipeline + REST API (port **8000**). OpenAPI at **`/docs`**. |
| **`favicon.svg`** | Tab icon (teal stress line on slate, aligned with the in-app logo). |

---

## Quick start

### Frontend (recommended for judges / local demo)

```bash
node dev-server.mjs
```

Open **http://127.0.0.1:5500/**.

By default, `/api/*` is proxied to the deployed hub:

- **`https://fuel-ireland.hub.zerve.cloud`**

Override the upstream host:

```bash
UPSTREAM_HOST=your-host.hub.zerve.cloud node dev-server.mjs
```

Optional: **`PORT`**, **`HOST`** (see `dev-server.mjs`).

**Use local FastAPI instead of the cloud hub**

1. Run the backend (below), then  
2. Before the page loads, set in the browser console:

   ```js
   window.__FFL_API_BASE__ = 'http://127.0.0.1:8000';
   ```

   Reload. The UI strips a trailing `/api` if present, so `http://127.0.0.1:8000` is correct.

**Direct `file://`**

Opening `index.html` from disk will break API calls (and often the proxy). Always use `dev-server.mjs` or another HTTP server for full behaviour.

### Backend (local FastAPI)

```bash
cd api && pip install -r requirements.txt && uvicorn main:app --host 0.0.0.0 --port 8000
```

On startup the pipeline may **fetch external APIs**; if they are unreachable, it falls back to bundled data. First boot can take a few seconds.

---

## How the dashboard talks to the API

- **Default public base** (when not using the local proxy): **`https://fuel-ireland.hub.zerve.cloud`**
- **`window.__FFL_API_BASE__`** overrides that base (e.g. local `uvicorn`).

### Batch counties (primary load path)

The UI loads **all 26 counties in one request**:

```http
GET /counties?fuel_price={price}
```

Example shape (deployed hub):

```json
{
  "fuel_price_queried": 2.14,
  "total_counties": 26,
  "counties_in_energy_poverty": 11,
  "total_vulnerable_households": 490130,
  "counties": [ /* per-county objects */ ]
}
```

The **stats strip** uses **`counties_in_energy_poverty`** and **`total_vulnerable_households`** from this response when present. Other views still use the per-county objects (tiers, charts, register).

### Local FastAPI `/counties` (difference)

The in-repo **`api/main.py`** exposes **`GET /counties`** as a **name list** for bootstrapping (`{"counties": [...], "count": N}`). For a **full batch** matching the cloud hub, run or deploy the **hub** that implements `GET /counties?fuel_price=...` with the aggregate fields above, or point the UI at that hub via `__FFL_API_BASE__` / `UPSTREAM_HOST`.

### Other routes the UI may call

Including but not limited to: **`/county/{county}`**, **`/scenario`**, **`/history`**, **`/deep-dive/{county}`**, **`/meta`**, **`/insights/*`**, **`/model/*`**, **`/national/snapshot`**, **`/compare/counties`**, **`/export/*`**. Exact availability depends on the hub you attach.

---

## App pages (sidebar)

- **Dashboard** — At-a-glance metrics, scenario curve, tier charts, stress trajectory, bubble and donut views.  
- **Scenarios** — Single diesel €/L slider or **compare A/B**; drives the batch county refresh.  
- **Compare** — Matrices-style charts and table for exploration.  
- **Counties** — Full county register and detail flow.  
- **Method & lab** — Lineage, OpenAPI link, model checks, sensitivity, exports when the API provides them.  
- **Demo & submit** — **`GET /insights/submission-pack`** (Devpost draft, video checklist, social copy, rubric hooks) when available.  
- **AI · Gemini** — In-browser **Google Generative Language API**; see below.

---

## AI chat (Google Gemini)

The **AI · Gemini** page calls **`generativelanguage.googleapis.com`** from the **browser** with a key you enter in the UI (stored in **`localStorage`** as **`ffl_gemini_key`**). That key is **not** sent to Fuel Fault Lines’ FastAPI.

Default model id in the page script is **`gemini-2.5-flash`** (change it if your key requires another model). Keys: [Google AI Studio](https://aistudio.google.com/apikey).

---

## API reference (local `api/main.py`)

Interactive docs: **`GET /docs`** (tags: core, county, scenario, model, insights, export).

| Method | Route | Purpose |
|--------|--------|---------|
| GET | `/health` | Liveness + version metadata when the model is initialised |
| GET | `/meta` | Lineage, params, limitations, judge/demo hints |
| GET | `/counties` | Sorted county **name list** + count (local app) |
| GET | `/county/{county}` | County snapshot at `fuel_price` |
| GET | `/scenario` | All counties at `price_a` vs `price_b` |
| GET | `/history` | Time series payload for the dashboard |
| GET | `/deep-dive/{county}` | Extended county payload |
| GET | `/compare/counties` | Side-by-side two counties + diff narrative fields |
| GET | `/export/county/{county}` | Markdown county brief |
| GET | `/export/briefing` | National markdown briefing |
| GET | `/national/snapshot` | Headline national stats (`price_eur_l`) |
| GET | `/insights/narrative` | Narrative bullets for comms |
| GET | `/insights/headline` | Composite headline + bullets |
| GET | `/insights/submission-pack` | Devpost-oriented pack (`price_eur_l`) |
| GET | `/insights/regional` | Province roll-ups (`fuel_price`) |
| GET | `/model/scenario-curve` | Counties over threshold vs €/L |
| GET | `/model/ranking-stability` | Weight-sensitivity of rankings |
| GET | `/model/claims` | Pass/fail style checks (`price_eur_l`) |
| GET | `/model/sensitivity` | Stress tests |
| GET | `/model/policy` | Illustrative grant scenarios |
| GET | `/model/validation` | Internal correlation checks |
| GET | `/model/distribution` | Deciles of fuel-income share |
| GET | `/model/breach-prices` | Per-county threshold-crossing prices |
| POST | `/model/params` | Session-local parameter tuning (rebuilds model unless using Zerve variable injection) |

---

## Zerve workflow

1. Build and iterate on the dataframe in a **Zerve** notebook (e.g. block `warmer_homes_roi`, variable `warmer_homes_df`).  
2. Deploy this repo’s **`api/`** as a FastAPI app on Zerve (or use the hosted hub).  
3. Optional: set **`USE_ZERVE_VARIABLE=1`** and **`ZERVE_DATA_BLOCK`** / **`ZERVE_DATA_VAR`** so the hub reads live notebook output.

Hackathon context: [ZerveHack on Devpost](https://zervehack.devpost.com) — public Zerve project, short summary, video, social tagging.

---

## Limitations (read the model, not the headline)

The model uses **proxies** and **synthetic income bands** where survey microdata is not available. Outputs are **not** official administrative fuel-poverty statistics. **`/meta`** and export briefs spell this out; treat numbers as **scenario illustrations** for policy exploration.

---

## Development notes

- **No `package.json`** — frontend dev server uses only Node built-ins.  
- **No configured linter or automated tests** in this repository.  
- **`AGENTS.md`** — extra notes for Cursor / cloud agents (architecture, ports, gotchas).

---

## Sources (data)

**SEAI** · **CSO** · **AA Ireland** (price snapshot) · **Zerve** model API — as surfaced in the app footer and pipeline.
