# AGENTS.md

## Cursor Cloud specific instructions

### Architecture overview

Fuel Fault Lines is a two-service application for visualizing Irish county-level energy/fuel poverty data:

- **FastAPI backend** (`api/`) — Python data pipeline + REST API (port 8000)
- **Frontend** (`index.html`) — Single-file vanilla HTML/JS dashboard served by `dev-server.mjs` (port 5500)
- **AI chat** — Optional **Google Gemini** assistant on the **AI · Gemini** page: browser-side calls to `generativelanguage.googleapis.com` with a user-supplied key (not sent to FastAPI).
- **Demo & submit** — UI page backed by `GET /insights/submission-pack` (Devpost draft ≤300 words, video shot list, social draft, rubric hooks).

No database — all data is computed in-memory from public APIs (SEAI, CSO) with hardcoded fallbacks.

### Running services

- **Backend:** `cd api && uvicorn main:app --host 0.0.0.0 --port 8000`
- **Frontend dev server:** `node dev-server.mjs` (serves on port 5500, proxies `/api/*` to Zerve cloud via HTTPS by default)
- The dev-server proxy uses HTTPS to reach the upstream. To use the local FastAPI instead of Zerve cloud, open the frontend with `window.__FFL_API_BASE__ = 'http://127.0.0.1:8000'` in the browser console, or access the FastAPI directly at port 8000.

### Key API endpoints (FastAPI)

`/health`, `/county/{county}`, `/scenario?price_a=X&price_b=Y`, `/history`, `/deep-dive/{county}`

### Gotchas

- The `dev-server.mjs` has zero npm dependencies (uses only Node.js built-ins). No `package.json` or `npm install` needed.
- The FastAPI startup fetches external data (SEAI, CSO); if those APIs are unreachable, it falls back to hardcoded data. Startup may take a few seconds.
- No linter or test framework is configured in this repository.
- No build step exists — both frontend (single HTML file) and backend run directly.
