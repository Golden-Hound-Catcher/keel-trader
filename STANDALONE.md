# Keel Trader Standalone Deployment (Stage 2)

Keel Trader is composed of:

- `keel.worker`: **the sole job scheduler** (15-minute trader paper/demo cycle, factor refresh, news, daily briefing, nightly backup).
- `keel.worker.cycle`: paper/demo vertical path — factors → decision → risk → execution → SQLite ledger (no shell OKX CLI).
- `r20_backend.app`: FastAPI control plane / admin. **Does not** auto-spawn a second scheduler.
- `r20_gateway.worker`: optional **notification delivery only** (job ticks disabled by default).
- `scripts/ai_factor_trader.py`: thin shim that delegates into `keel.worker.cycle` unless `KEEL_USE_LEGACY=1`.

## Install

```sh
python3 -m venv .venv
. .venv/bin/activate
pip install -r requirements.txt
cp env.example .env
chmod 600 .env
```

Set `LLM_*` and `OKX_*` credentials in `.env` if you later enable live/demo exchange adapters. Paper cycle needs no exchange credentials.

## Run Locally (correct Stage 2 topology)

Terminal 1 — **sole scheduler**:

```sh
. .venv/bin/activate
python -m keel.worker
# or one-shot: python -m keel.worker --once
```

Terminal 2 — control plane (no second scheduler):

```sh
. .venv/bin/activate
python -m uvicorn r20_backend.app:app --host 0.0.0.0 --port 8080
```

Optional Terminal 3 — notification delivery only:

```sh
. .venv/bin/activate
python -m r20_gateway.worker   # KEEL_DISABLE_GATEWAY_SCHEDULER implied / jobs off
```

## Do NOT run two schedulers

| Unit / module | Status |
|---------------|--------|
| `python -m keel.worker` / `deploy/keel-worker.service` | ✅ use this |
| `r20_backend.scheduler` / `deploy/r20-scheduler.service` | ❌ disabled (exits) |
| Backend lifespan gateway auto-spawn | ❌ removed |
| Gateway `GatewayScheduler.tick` | ❌ off unless `KEEL_ENABLE_LEGACY_GATEWAY_SCHEDULER=1` |

## systemd

Copy `deploy/keel-worker.service` and `deploy/r20-quantum.service` (and optionally `deploy/r20-gateway.service` for notifications):

```sh
sudo systemctl daemon-reload
sudo systemctl disable --now r20-scheduler.service || true
sudo systemctl enable --now keel-worker r20-quantum
```

Never enable `r20-scheduler.service` alongside `keel-worker`.
