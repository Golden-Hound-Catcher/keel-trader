# Deploy units

## Recommended one-path

```sh
./deploy/install.sh                 # venv + .env + rewritten *.service.local
# edit .env (KEEL_OKX_* demo keys if needed; chmod 600)
INSTALL_SYSTEMD=1 ./deploy/install.sh   # copies units (sudo if not root)
sudo systemctl enable --now keel-api keel-worker
./scripts/run_acceptance.sh         # paper gate (no keys)
# optional: ./scripts/ops_smoke.sh  # acceptance + soft /health check
```

Ops narrative: [`../RUNBOOK.md`](../RUNBOOK.md).

`install.sh` rewrites `WorkingDirectory` / `EnvironmentFile` / `PATH` to the repo root
into `deploy/*.service.local`. With `INSTALL_SYSTEMD=1` as root it copies into
`/etc/systemd/system` and runs `daemon-reload`; without root it prints the exact
`sudo` commands. Edit `User=` / `Group=` on the units if your host is not `r20`.

## Supported (enable these only)

| Unit | Starts | Role |
|------|--------|------|
| `keel-worker.service` | `python -m keel.worker` | **Sole** scheduler |
| `keel-api.service` | `uvicorn keel.api.app:app` | **Primary** read-only API |

Optional UI: build/serve `frontend/` (Phase U1 monitor) against `keel-api`. See `frontend/README.md`.

```sh
# Disable any leftover units from older installs (unit files no longer shipped)
sudo systemctl disable --now r20-scheduler.service r20-quantum.service r20-gateway.service || true
sudo cp deploy/keel-api.service.local deploy/keel-worker.service.local /etc/systemd/system/
# Or: INSTALL_SYSTEMD=1 ./deploy/install.sh
sudo systemctl daemon-reload
sudo systemctl enable --now keel-worker keel-api
```

## Removed legacy units

`r20-quantum.service`, `r20-scheduler.service`, and `r20-gateway.service` are
**removed** from the tree (packages `r20_backend/` and `r20_gateway/` deleted).
Inventory: [`../LEGACY.md`](../LEGACY.md). Do not run two schedulers. See
[`../STANDALONE.md`](../STANDALONE.md).

Supported entrypoints only: `uvicorn keel.api.app:app` + `python -m keel.worker`
(+ optional Vite monitor).
