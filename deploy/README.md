# Deploy units

## Supported (enable these only)

| Unit | Starts | Role |
|------|--------|------|
| `keel-worker.service` | `python -m keel.worker` | **Sole** scheduler |
| `keel-api.service` | `uvicorn keel.api.app:app` | **Primary** read-only API |

Optional UI: build/serve `frontend/` (Phase U1 monitor) against `keel-api`. See `frontend/README.md`.

```sh
# Disable any leftover units from older installs (unit files no longer shipped)
sudo systemctl disable --now r20-scheduler.service r20-quantum.service r20-gateway.service || true
sudo cp deploy/keel-worker.service deploy/keel-api.service /etc/systemd/system/
# Edit WorkingDirectory / User / EnvironmentFile paths to match your install
sudo systemctl daemon-reload
sudo systemctl enable --now keel-worker keel-api
```

`deploy/install.sh` prepares the venv + `.env` for this Keel topology.

## Removed legacy units

`r20-quantum.service`, `r20-scheduler.service`, and `r20-gateway.service` are
**removed** from the tree (packages `r20_backend/` and `r20_gateway/` deleted).
Inventory: [`../LEGACY.md`](../LEGACY.md). Do not run two schedulers. See
[`../STANDALONE.md`](../STANDALONE.md).

Supported entrypoints only: `uvicorn keel.api.app:app` + `python -m keel.worker`
(+ optional Vite monitor).
