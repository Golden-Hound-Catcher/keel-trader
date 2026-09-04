# Keel Trader Standalone Deployment

**Supported long-running processes (only):**

| Process | Module / unit | Role |
|---------|---------------|------|
| **keel-api** | `uvicorn keel.api.app:app` / `deploy/keel-api.service` | Primary read-only API control plane |
| **keel-worker** | `python -m keel.worker` / `deploy/keel-worker.service` | **Sole** job scheduler + paper/demo cycle |

**Supported UI (U1):** `frontend/` monitor bound to Keel `/health` + `/api/v1/*` (see `frontend/README.md`).

`r20_*` packages and old `r20-*.service` units are **removed**. Jinja `dashboard/` was removed in Phase U2. Inventory: [`LEGACY.md`](LEGACY.md).
New installs enable **only** `keel-api` + `keel-worker`.

## Components

- `keel.api.app`: **primary** FastAPI entry (health + `/api/v1/*` read-only).
- `keel.worker`: **sole** scheduler (trader paper/demo cycle).
- `keel.worker.cycle`: paper/demo vertical path — factors → decision → risk → execution → SQLite ledger (no shell OKX CLI).
- `frontend/`: Phase U1 monitor UI (client of Keel API only).
- `r20_backend` / `r20_gateway`: **removed** (use `keel.api` + `keel.notify`).

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

## Do NOT run two schedulers

| Unit / module | Status |
|---------------|--------|
| `python -m keel.worker` / `deploy/keel-worker.service` | **supported** |
| `uvicorn keel.api.app:app` / `deploy/keel-api.service` | **supported** |
| `frontend/` U1 monitor | **supported** (read-only client) |
| `r20_backend` / `r20_gateway` / `deploy/r20-*.service` | **removed** |

## systemd

Install **only** Keel units:

```sh
sudo cp deploy/keel-worker.service deploy/keel-api.service /etc/systemd/system/
# Edit WorkingDirectory / User / EnvironmentFile paths to match your install
sudo systemctl daemon-reload
sudo systemctl disable --now r20-scheduler.service r20-quantum.service r20-gateway.service || true
sudo systemctl enable --now keel-worker keel-api
```

Former `r20-*.service` unit files are no longer shipped; disable any leftover
units from older installs, then enable only `keel-worker` + `keel-api`.
