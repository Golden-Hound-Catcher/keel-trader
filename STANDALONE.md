# Keel Trader Standalone Deployment

**Supported long-running processes (only):**

| Process | Module / unit | Role |
|---------|---------------|------|
| **keel-api** | `uvicorn keel.api.app:app` / `deploy/keel-api.service` | Primary read-only API control plane |
| **keel-worker** | `python -m keel.worker` / `deploy/keel-worker.service` | **Sole** job scheduler + paper/demo cycle |

**Supported UI (U1):** `frontend/` monitor bound to Keel `/health` + `/api/v1/*` (see `frontend/README.md`).

Everything under `r20_*` is **legacy / deprecated**. Jinja `dashboard/` was **removed in Phase U2**. Inventory: [`LEGACY.md`](LEGACY.md).
New installs enable **only** `keel-api` + `keel-worker`. `r20-*` units require opt-in marker files under `data/` and are **not** install examples.

## Components

- `keel.api.app`: **primary** FastAPI entry (health + `/api/v1/*` read-only).
- `keel.worker`: **sole** scheduler (15-minute trader paper/demo cycle, factor refresh, news, daily briefing, nightly backup).
- `keel.worker.cycle`: paper/demo vertical path — factors → decision → risk → execution → SQLite ledger (no shell OKX CLI).
- `frontend/`: Phase U1 monitor UI (client of Keel API only).
- `r20_backend.app`: **LEGACY 410 stub** — not a supported deployment entrypoint. Soft-blocked unless `KEEL_ALLOW_LEGACY_BACKEND=1`. Admin HTTP API removed; any path returns 410. Prefer `keel.api.app`.
- `r20_gateway`: **removed** (entire package deleted; use `keel.notify`).

## Install

```sh
python3 -m venv .venv
. .venv/bin/activate
pip install -r requirements.txt
cp env.example .env
chmod 600 .env
# or: ./deploy/install.sh
```

Set `LLM_*` and `OKX_*` credentials in `.env` if you later enable live/demo exchange adapters. Paper cycle needs no exchange credentials.

## Run Locally

Terminal 1 — **sole scheduler**:

```sh
. .venv/bin/activate
python -m keel.worker
# or one-shot: python -m keel.worker --once
```

Terminal 2 — **primary API**:

```sh
. .venv/bin/activate
python -m uvicorn keel.api.app:app --host 0.0.0.0 --port 8080
```

Terminal 3 (optional) — **U1 monitor**:

```sh
cd frontend && npm ci && npm run dev
```

### Legacy (blocked by default — do not use for new deploys)

```sh
# Requires KEEL_ALLOW_LEGACY_BACKEND=1; conflicts with keel-api on the same port
# 410 stub only — r20_gateway package removed
KEEL_ALLOW_LEGACY_BACKEND=1 python -m uvicorn r20_backend.app:app --host 0.0.0.0 --port 8080
```

Without `KEEL_ALLOW_LEGACY_BACKEND=1`, importing/serving `r20_backend.app` exits with code `2` and points at `keel.api`.

## Do NOT run two schedulers

| Unit / module | Status |
|---------------|--------|
| `python -m keel.worker` / `deploy/keel-worker.service` | **supported** |
| `uvicorn keel.api.app:app` / `deploy/keel-api.service` | **supported** |
| `frontend/` U1 monitor | **supported** (read-only client) |
| `r20_backend.app` / `deploy/r20-quantum.service` | legacy; gated + soft-blocked (410 stub) |
| `r20_backend.scheduler` / `deploy/r20-scheduler.service` | disabled (exits / cannot start) |
| `r20_gateway` / `deploy/r20-gateway.service` | **removed** |
| Backend lifespan gateway auto-spawn | removed |

## systemd

Install **only** Keel units:

```sh
sudo cp deploy/keel-worker.service deploy/keel-api.service /etc/systemd/system/
# Edit WorkingDirectory / User / EnvironmentFile paths to match your install
sudo systemctl daemon-reload
sudo systemctl disable --now r20-scheduler.service r20-quantum.service || true
sudo systemctl enable --now keel-worker keel-api
```

Never enable `r20-scheduler.service` alongside `keel-worker`.
`r20-quantum.service` is a gated leftover (410 stub); `r20-gateway.service` was removed with the package. Not install examples.
