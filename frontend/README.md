# Keel Trader Frontend — Monitor only

Monitor-only Vue UI bound to Keel keel.api.app (/health + /api/v1/*).

Jinja dashboard/, Vue /legacy, R20 Vue /admin/*, and r20_backend /api/v1/admin/* are removed. Admin features deferred to a future Keel admin API (SPEC addendum).

## Forbidden (U1 path)

- Reading data/*.json
- Calling legacy r20_backend non-public routes from the monitor store
- Attaching R20 session headers on Keel monitor reads (v1 local/demo has no auth)

## Run (dev)

Terminal 1 — Keel API (repo root, venv + .env):

    python -m uvicorn keel.api.app:app --host 0.0.0.0 --port 8080

Optional worker (fills ledger):

    python -m keel.worker --once

Terminal 2 — Vite:

    cd frontend
    npm ci
    npm run dev

Vite proxies /api, /health, /ready, /docs, /openapi.json to http://127.0.0.1:8080.

## Routes

- / or /monitor — Keel monitor (useKeelApi + stores/monitor)
- /admin/* — gone (not a product surface)

## API mapping (U1)

- health <- GET /health
- status <- GET /api/v1/status
- balance <- GET /api/v1/balance
- positions <- GET /api/v1/positions
- decisions <- GET /api/v1/decisions
- trades <- GET /api/v1/trades
- events <- GET /api/v1/events
- factors <- GET /api/v1/factors/{inst_id}

## Build

    cd frontend && npm ci && npm run build

When frontend/dist exists, keel.api may mount it as a static SPA (see keel/api/app.py).

## Key files

- src/composables/useKeelApi.ts — monitor client (no R20 session header)
- src/stores/monitor.ts — aggregates Keel endpoints
- src/views/MonitorView.vue — monitor UI
