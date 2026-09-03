# Deploy units (Stage 3)

## Recommended

| Unit | Starts | Role |
|------|--------|------|
| `keel-worker.service` | `python -m keel.worker` | **Sole** scheduler |
| `keel-api.service` | `uvicorn keel.api.app:app` | **Primary** read-only API |

```sh
sudo systemctl disable --now r20-scheduler.service r20-quantum.service || true
sudo systemctl enable --now keel-worker keel-api
```

## Legacy / deprecated

| Unit | Status |
|------|--------|
| `r20-quantum.service` | Legacy admin + read-only dashboard UI (`r20_backend.app`) |
| `r20-gateway.service` | Optional notification delivery only |
| `r20-scheduler.service` | **DISABLED** — `ConditionPathExists` + `/bin/false` |

Do not run two schedulers. See `../STANDALONE.md`.
