# Legacy inventory (quarantine after admin API removal)

Keel Trader keeps historical R20 helper modules in-tree so gateway/scripts and
rollback paths still work. **The Vue `/admin/*` UI, Jinja `dashboard/`, Vue
`/legacy`, `admin_auth`, and `/api/v1/admin/*` HTTP routes are gone.**
`r20_backend.app` is a soft-blocked **410 stub** only.
**New deployments must not use legacy entrypoints.**

## Supported runtime (only)

| Process | Module / unit |
|---------|----------------|
| API | `uvicorn keel.api.app:app` / `deploy/keel-api.service` |
| Scheduler | `python -m keel.worker` / `deploy/keel-worker.service` |
| Monitor UI | `frontend/` Vite app -> Keel `/health` + `/api/v1/*` (routes `/`, `/monitor`) |

Opt-in flags:

| Env | Purpose |
|-----|---------|
| `KEEL_USE_LEGACY=1` | Acknowledge legacy scripts; silence import quarantine warnings |
| `KEEL_ALLOW_LEGACY_BACKEND=1` | **Required** to import/serve `r20_backend.app` (410 stub only) |
| `KEEL_ENABLE_LEGACY_GATEWAY_SCHEDULER=1` | Emergency: re-enable gateway job ticks |

Gateway job ticks remain separately gated. Prefer Keel units only.

---

## Retirement status

| Stage | Status |
|-------|--------|
| Shim traders -> Keel cycle; kill dual schedulers; quarantine warnings | **Done** |
| Phase U1 — Vue monitor rebound to Keel API | **Done** |
| Soft-block `r20_backend.app` as a deployment entrypoint | **Done** |
| Phase U2 — drop Jinja `dashboard/` + Vue `/legacy` | **Done** |
| Remove Vue `/admin/*` product surface + `admin.html` | **Done** |
| Remove `r20_backend` `/api/v1/admin/*` + `admin_auth` | **Done** |
| Delete unused scripts / `r20_gateway` scheduler / remaining helpers | Later (inventory-gated) |

---

## Packages (kept, hard to misuse)

| Path | Why it remains | Accidental-use guard |
|------|----------------|----------------------|
| `r20_backend/` | Helper modules for optional gateway/scripts (notifications, llm_manager, backup_*, OKX helpers, council, qq_*, etc.) | Import warn; prefer `keel.api` |
| `r20_backend/app.py` | Soft-blocked **410 stub** (admin HTTP API removed) | **`KEEL_ALLOW_LEGACY_BACKEND=1` required** to import via uvicorn; else exit `2` |
| `r20_backend/scheduler.py` | Hard guard against double-firing | Immediate exit code `2` |
| `r20_gateway/` | Optional notification delivery | Import warn; no job ticks by default |
| `r20_gateway/worker.py` | Notify-only loop | Loud stderr warn on `run()`; scheduler off |
| `r20_gateway/scheduler.py` | Unit tests / emergency rollback | Off unless `KEEL_ENABLE_LEGACY_GATEWAY_SCHEDULER=1` |

### Removed from `r20_backend`

| Path | Status |
|------|--------|
| `r20_backend/admin_auth.py` | **Deleted** |
| `r20_backend/app.py` admin FastAPI routes (`/api/v1/admin/*`, HTML `/admin`) | **Deleted** (replaced by 410 stub) |
| `r20_backend/admin.html` | **Deleted** (prior PR) |

Keel code does **not** import these helpers; they remain for `r20_gateway` and historical `scripts/` only.

## Deploy units

Install examples / enable only `keel-*.service`. All `r20-*.service` stay disabled/gated.

| Unit | Default | Notes |
|------|---------|-------|
| `deploy/keel-api.service` | **enable** | Primary API (+ optional `frontend/dist` SPA) |
| `deploy/keel-worker.service` | **enable** | Sole scheduler |
| `deploy/r20-quantum.service` | **disabled** | Gated; would serve the 410 stub only — prefer keel-api |
| `deploy/r20-gateway.service` | **disabled** | Needs `data/.enable_legacy_r20_gateway` |
| `deploy/r20-scheduler.service` | **disabled** | `ConditionPathExists` + `/bin/false` |

## Scripts (shim or historical)

| Path | Status |
|------|--------|
| `scripts/ai_factor_trader.py` | Defaults to `keel.worker.cycle`; legacy OKX-CLI loop only if `KEEL_USE_LEGACY=1` |
| `scripts/ai_brain_trader.py` | Legacy brain loop; warns unless `KEEL_USE_LEGACY=1` (optional `KEEL_BRAIN_SHIM=1` -> cycle) |
| `scripts/daemon_web_sync.py` | Legacy console sync daemon |
| `scripts/sync_web_data.py` | Historical R20 dashboard cache generator (dashboard package gone) |
| `scripts/daily_summary_and_backup.py` | Historical briefing job (worker owns schedule) |
| `scripts/self_improvement_engine.py` | Historical evolution job |
| `scripts/nightly_backup_and_clean.py` | Historical nightly job |
| `scripts/qq_notifier.py` | Bridge into gateway events |
| `r20_backend/qq_gateway_daemon.py` | Legacy QQ daemon helper |

## Removed UI / admin artifacts

| Path | Status |
|------|--------|
| `dashboard/` | **Deleted** (Jinja + static JS + start/stop) |
| Vue `/legacy` + `DashboardView` + R20 `/api/all` shell components | **Deleted** |
| Vue `/admin/*` + `AdminLayout` + `views/admin/**` + auth/`useApi` | **Deleted** |
| `frontend/public/admin/legacy.html` | **Deleted** |
| `r20_backend/admin.html` | **Deleted** |
| `r20_backend/admin_auth.py` | **Deleted** |
| `/api/v1/admin/*` HTTP routes | **Deleted** (stub returns 410) |
| `frontend/` monitor | **Supported** client of `keel.api` (`/`, `/monitor`) |

Physical `rm -rf` of remaining `r20_*` helper packages is **out of scope** until gateway/scripts no longer need them (SPEC later stage).

## Verification

```sh
python -m r20_backend.scheduler          # must exit 2
python -c "import r20_backend.app"       # must exit 2 without KEEL_ALLOW_LEGACY_BACKEND=1
KEEL_ALLOW_LEGACY_BACKEND=1 python -c "import r20_backend.app"  # opt-in ok (410 stub only)
test ! -f r20_backend/admin_auth.py
test ! -d dashboard
test ! -d frontend/src/views/admin
make test                                # Keel core tests
python -m pytest tests/test_legacy_quarantine.py -v
```
