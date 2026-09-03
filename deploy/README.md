# Deploy units (Stage 7)

## Recommended (enable these only)

| Unit | Starts | Role |
|------|--------|------|
| `keel-worker.service` | `python -m keel.worker` | **Sole** scheduler |
| `keel-api.service` | `uvicorn keel.api.app:app` | **Primary** read-only API |

```sh
sudo systemctl disable --now r20-scheduler.service r20-quantum.service r20-gateway.service || true
sudo systemctl enable --now keel-worker keel-api
```

## Legacy / deprecated (disabled by default)

| Unit | Status | Re-enable marker (opt-in) |
|------|--------|---------------------------|
| `r20-quantum.service` | **ConditionPathExists** gate | `data/.enable_legacy_r20_quantum` |
| `r20-gateway.service` | **ConditionPathExists** gate | `data/.enable_legacy_r20_gateway` |
| `r20-scheduler.service` | **DISABLED** — `ConditionPathExists` + `/bin/false` | never (use keel-worker) |

New installs should **not** enable any `r20-*.service`. Inventory and rationale: [`../LEGACY.md`](../LEGACY.md).
Do not run two schedulers. See [`../STANDALONE.md`](../STANDALONE.md).
