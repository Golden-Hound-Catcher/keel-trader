# Keel Trader Standalone Deployment (Stage 3)

**Run only two processes in normal operation:**

| Process | Module / unit | Role |
|---------|---------------|------|
| **keel-api** | `uvicorn keel.api.app:app` / `deploy/keel-api.service` | Primary read-only API control plane |
| **keel-worker** | `python -m keel.worker` / `deploy/keel-worker.service` | **Sole** job scheduler + paper/demo cycle |

Everything under `r20_*` is **legacy / deprecated**.

## Components

- `keel.api.app`: **primary** FastAPI entry (health + `/api/v1/*` read-only).
- `keel.worker`: **sole** scheduler (15-minute trader paper/demo cycle, factor refresh, news, daily briefing, nightly backup).
- `keel.worker.cycle`: paper/demo vertical path — factors → decision → risk → execution → SQLite ledger (no shell OKX CLI).
- `r20_backend.app`: **LEGACY** admin/control plane. Mounts `dashboard` as a **legacy read-only UI** at `/`. Does **not** auto-spawn a second scheduler. Prefer `keel.api.app` for new deployments.
- `r20_gateway.worker`: **LEGACY** optional notification delivery only (job ticks disabled by default).
- `scripts/ai_factor_trader.py`: thin shim → `keel.worker.cycle` unless `KEEL_USE_LEGACY=1`.

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

## Run Locally (Stage 3 topology)

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

### Legacy (optional, same port conflict — do not dual-bind)

```sh
# LEGACY control plane + read-only dashboard UI only
python -m uvicorn r20_backend.app:app --host 0.0.0.0 --port 8080

# LEGACY notification delivery only
python -m r20_gateway.worker
```

## Do NOT run two schedulers

| Unit / module | Status |
|---------------|--------|
| `python -m keel.worker` / `deploy/keel-worker.service` | use this |
| `uvicorn keel.api.app:app` / `deploy/keel-api.service` | use this |
| `r20_backend.app` / `deploy/r20-quantum.service` | legacy UI/admin only |
| `r20_backend.scheduler` / `deploy/r20-scheduler.service` | disabled (exits / cannot start) |
| Backend lifespan gateway auto-spawn | removed |
| Gateway `GatewayScheduler.tick` | off unless `KEEL_ENABLE_LEGACY_GATEWAY_SCHEDULER=1` |

## systemd

Prefer Keel units only:

```sh
sudo cp deploy/keel-worker.service deploy/keel-api.service /etc/systemd/system/
# Edit WorkingDirectory / User / EnvironmentFile paths to match your install
sudo systemctl daemon-reload
sudo systemctl disable --now r20-scheduler.service r20-quantum.service || true
sudo systemctl enable --now keel-worker keel-api
```

Never enable `r20-scheduler.service` alongside `keel-worker`.
`r20-quantum.service` and `r20-gateway.service` remain available only for legacy admin UI / notifications.
