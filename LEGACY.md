# Legacy inventory (quarantine after U2)

Keel Trader keeps historical R20 paths in-tree so rollback and transitional
`/admin/*` control-plane routes still work. **New deployments must not use them.**
Jinja `dashboard/` and the Vue `/legacy` route were **removed in Phase U2**.

## Supported runtime (only)

| Process | Module / unit |
|---------|----------------|
| API | `uvicorn keel.api.app:app` / `deploy/keel-api.service` |
| Scheduler | `python -m keel.worker` / `deploy/keel-worker.service` |
| Monitor UI (U1/U2) | `frontend/` Vite app → Keel `/health` + `/api/v1/*` |

Opt-in flags:

| Env | Purpose |
|-----|---------|
| `KEEL_USE_LEGACY=1` | Acknowledge legacy scripts; silence import quarantine warnings |
| `KEEL_ALLOW_LEGACY_BACKEND=1` | **Required** to run `uvicorn r20_backend.app:app` (soft-block otherwise) — admin remnant only |
| `KEEL_ENABLE_LEGACY_GATEWAY_SCHEDULER=1` | Emergency: re-enable gateway job ticks |

Gateway job ticks remain separately gated. Prefer Keel units only.

---

## Retirement status

| Stage | Status |
|-------|--------|
| Shim traders → Keel cycle; kill dual schedulers; quarantine warnings | **Done** |
| Phase U1 — Vue monitor rebound to Keel API | **Done** |
| Soft-block `r20_backend.app` as a deployment entrypoint | **Done** |
| Phase U2 — drop Jinja `dashboard/` + Vue `/legacy` | **Done** |
| Delete unused scripts / `r20_gateway` scheduler / `r20_backend` | Last |

---

## Packages (kept, hard to misuse)

| Path | Why it remains | Accidental-use guard |
|------|----------------|----------------------|
| `r20_backend/` | **Admin-only remnant** (prompt/LLM/backup/gateway admin APIs for `/admin/*`) — no dashboard mount | Import warn; prefer `keel.api` |
| `r20_backend/app.py` | Soft-blocked ASGI admin control plane (U2: dashboard unmounted) | **`KEEL_ALLOW_LEGACY_BACKEND=1` required** to run via uvicorn; else exit `2` |
| `r20_backend/scheduler.py` | Hard guard against double-firing | Immediate exit code `2` |
| `r20_gateway/` | Optional notification delivery | Import warn; no job ticks by default |
| `r20_gateway/worker.py` | Notify-only loop | Loud stderr warn on `run()`; scheduler off |
| `r20_gateway/scheduler.py` | Unit tests / emergency rollback | Off unless `KEEL_ENABLE_LEGACY_GATEWAY_SCHEDULER=1` |

## Deploy units

Install examples / enable only `keel-*.service`. All `r20-*.service` stay disabled/gated.

| Unit | Default | Notes |
|------|---------|-------|
| `deploy/keel-api.service` | **enable** | Primary API (+ optional `frontend/dist` SPA) |
| `deploy/keel-worker.service` | **enable** | Sole scheduler |
| `deploy/r20-quantum.service` | **disabled** | Needs `data/.enable_legacy_r20_quantum` **and** sets `KEEL_ALLOW_LEGACY_BACKEND=1` (admin remnant) |
| `deploy/r20-gateway.service` | **disabled** | Needs `data/.enable_legacy_r20_gateway` |
| `deploy/r20-scheduler.service` | **disabled** | `ConditionPathExists` + `/bin/false` |

## Scripts (shim or historical)

| Path | Status |
|------|--------|
| `scripts/ai_factor_trader.py` | Defaults to `keel.worker.cycle`; legacy OKX-CLI loop only if `KEEL_USE_LEGACY=1` |
| `scripts/ai_brain_trader.py` | Legacy brain loop; warns unless `KEEL_USE_LEGACY=1` (optional `KEEL_BRAIN_SHIM=1` → cycle) |
| `scripts/daemon_web_sync.py` | Legacy console sync daemon |
| `scripts/sync_web_data.py` | Historical R20 dashboard cache generator (dashboard package gone) |
| `scripts/daily_summary_and_backup.py` | Historical briefing job (worker owns schedule) |
| `scripts/self_improvement_engine.py` | Historical evolution job |
| `scripts/nightly_backup_and_clean.py` | Historical nightly job |
| `scripts/qq_notifier.py` | Bridge into gateway events |
| `r20_backend/qq_gateway_daemon.py` | Legacy QQ daemon helper |

## Removed in U2 / remaining UI artifacts

| Path | Status |
|------|--------|
| `dashboard/` | **Deleted** (Jinja + static JS + start/stop) |
| Vue `/legacy` + `DashboardView` + R20 `/api/all` shell components | **Deleted** |
| `docs/images/*` (v540/v600 release cards, etc.) | Historical marketing / release screenshots |
| `frontend/public/admin/legacy.html` | Static legacy admin stub |
| `frontend/` monitor (U1/U2) | **Supported** client of `keel.api` |
| `frontend/` `/admin/*` | Transitional; labeled legacy; needs `r20_backend` opt-in |

Physical `rm -rf` of remaining `r20_*` is **out of scope** until admin APIs migrate to Keel.

## Verification

```sh
python -m r20_backend.scheduler          # must exit 2
python -c "import r20_backend.app"       # must exit 2 without KEEL_ALLOW_LEGACY_BACKEND=1
KEEL_ALLOW_LEGACY_BACKEND=1 python -c "import r20_backend.app"  # opt-in ok (no dashboard)
test ! -d dashboard                      # U2 removed
make test                                # Keel core tests
python -m pytest tests/test_legacy_quarantine.py -v
```
