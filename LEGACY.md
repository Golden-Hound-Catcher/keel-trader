# Legacy inventory (Stage 7 quarantine)

Keel Trader keeps historical R20 paths in-tree so rollback and the transitional
admin/dashboard mount still work. **New deployments must not use them.**

Primary runtime:

| Process | Module / unit |
|---------|----------------|
| API | `uvicorn keel.api.app:app` / `deploy/keel-api.service` |
| Scheduler | `python -m keel.worker` / `deploy/keel-worker.service` |

Opt-in: set `KEEL_USE_LEGACY=1` only when you intentionally run a legacy path
(silences import/run quarantine warnings). Gateway job ticks remain separately
gated by `KEEL_ENABLE_LEGACY_GATEWAY_SCHEDULER=1` (emergency only).

---

## Packages (kept, hard to misuse)

| Path | Why it remains | Accidental-use guard |
|------|----------------|----------------------|
| `r20_backend/` | Admin routes + mounts legacy `dashboard/` | Import warn; prefer `keel.api` |
| `r20_backend/app.py` | Still mounts dashboard UI — **not deleted** | Module docstring + import warn |
| `r20_backend/scheduler.py` | Hard guard against double-firing | Immediate exit code `2` |
| `r20_gateway/` | Optional notification delivery | Import warn; no job ticks by default |
| `r20_gateway/worker.py` | Notify-only loop | Loud stderr warn on `run()`; scheduler off |
| `r20_gateway/scheduler.py` | Unit tests / emergency rollback | Off unless `KEEL_ENABLE_LEGACY_GATEWAY_SCHEDULER=1` |

## Deploy units

| Unit | Default | Notes |
|------|---------|-------|
| `deploy/keel-api.service` | **enable** | Primary API |
| `deploy/keel-worker.service` | **enable** | Sole scheduler |
| `deploy/r20-quantum.service` | **disabled** | Needs `data/.enable_legacy_r20_quantum` |
| `deploy/r20-gateway.service` | **disabled** | Needs `data/.enable_legacy_r20_gateway` |
| `deploy/r20-scheduler.service` | **disabled** | `ConditionPathExists` + `/bin/false` |

## Scripts (shim or historical)

| Path | Status |
|------|--------|
| `scripts/ai_factor_trader.py` | Defaults to `keel.worker.cycle`; legacy OKX-CLI loop only if `KEEL_USE_LEGACY=1` |
| `scripts/ai_brain_trader.py` | Legacy brain loop; warns unless `KEEL_USE_LEGACY=1` (optional `KEEL_BRAIN_SHIM=1` → cycle) |
| `scripts/daemon_web_sync.py` | Legacy console sync daemon |
| `scripts/daily_summary_and_backup.py` | Historical briefing job (worker owns schedule) |
| `scripts/self_improvement_engine.py` | Historical evolution job |
| `scripts/nightly_backup_and_clean.py` | Historical nightly job |
| `scripts/qq_notifier.py` | Bridge into gateway events |
| `r20_backend/qq_gateway_daemon.py` | Legacy QQ daemon helper |

## Dashboard / release-note artifacts (documented, not mass-deleted)

| Path | Why it remains |
|------|----------------|
| `dashboard/` | Mounted by `r20_backend.app` as read-only legacy UI |
| `dashboard/start.sh` / `stop.sh` | Old container-oriented lifecycle; **do not use** on Keel hosts |
| `docs/images/*` (v540/v600 release cards, etc.) | Historical marketing / release screenshots |
| `frontend/public/admin/legacy.html` | Static legacy admin stub |

Physical `rm -rf` of `r20_*` is **out of scope** for Stage 7. Parent will
decide live-demo keys vs mass delete next.

## Verification

```sh
python -m r20_backend.scheduler          # must exit 2
python -m r20_gateway.worker             # notify-only; no trader job ticks
make test                                # Keel core tests
```
