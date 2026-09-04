# Deploy units

## Supported (enable these only)

| Unit | Starts | Role |
|------|--------|------|
| `keel-worker.service` | `python -m keel.worker` | **Sole** scheduler |
| `keel-api.service` | `uvicorn keel.api.app:app` | **Primary** read-only API |

Optional UI: build/serve `frontend/` (Phase U1 monitor) against `keel-api`. See `frontend/README.md`.

```sh
sudo systemctl disable --now r20-scheduler.service r20-quantum.service r20-gateway.service || true
sudo cp deploy/keel-worker.service deploy/keel-api.service /etc/systemd/system/
# Edit WorkingDirectory / User / EnvironmentFile paths to match your install
sudo systemctl daemon-reload
sudo systemctl enable --now keel-worker keel-api
```

`deploy/install.sh` prepares the venv + `.env` for this Keel topology. It does **not** enable any `r20-*.service`.

## Legacy / deprecated (disabled by default — not install examples)

| Unit | Status | Re-enable marker (opt-in) |
|------|--------|---------------------------|
| `r20-quantum.service` | **ConditionPathExists** gate + `KEEL_ALLOW_LEGACY_BACKEND=1` | `data/.enable_legacy_r20_quantum` |
| `r20-scheduler.service` | **DISABLED** — `ConditionPathExists` + `/bin/false` | never (use keel-worker; aligns with soft-blocked `r20_backend.scheduler`) |
| `r20-gateway.service` | **removed** (package `r20_gateway/` deleted) | n/a |

New installs should **not** enable any `r20-*.service`. Inventory and rationale: [`../LEGACY.md`](../LEGACY.md).
Do not run two schedulers. See [`../STANDALONE.md`](../STANDALONE.md).

`uvicorn r20_backend.app:app` is soft-blocked unless `KEEL_ALLOW_LEGACY_BACKEND=1`; even then it is a 410 stub (admin API removed). Prefer `keel-api.service`.
