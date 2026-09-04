# Legacy inventory (after O3 hard-delete of `r20_*`)

Keel Trader no longer ships `r20_backend/`, `r20_gateway/`, or the old
`r20-*.service` units. Jinja `dashboard/`, Vue `/legacy`, Vue `/admin/*`,
`admin_auth`, and `/api/v1/admin/*` were removed earlier. **New deployments
use Keel entrypoints only.**

## Supported runtime (only)

| Process | Module / unit |
|---------|----------------|
| API | `uvicorn keel.api.app:app` / `deploy/keel-api.service` |
| Scheduler | `python -m keel.worker` / `deploy/keel-worker.service` |
| Monitor UI | `frontend/` Vite app -> Keel `/health` + `/api/v1/*` (routes `/`, `/monitor`) |

`KEEL_ALLOW_LEGACY_BACKEND` and `KEEL_USE_LEGACY` are **obsolete** (no remaining
soft-block stubs). `KEEL_ENABLE_LEGACY_GATEWAY_SCHEDULER` is obsolete —
`r20_gateway` was removed. Keel scheduler is **trader-only**
(`python -m keel.worker`).

---

## Retirement status

| Stage | Status |
|-------|--------|
| Shim traders -> Keel cycle; kill dual schedulers; quarantine warnings | **Done** |
| Phase U1 — Vue monitor rebound to Keel API | **Done** |
| Soft-block then hard-delete `r20_backend.app` | **Done** |
| Phase U2 — drop Jinja `dashboard/` + Vue `/legacy` | **Done** |
| Remove Vue `/admin/*` product surface + `admin.html` | **Done** |
| Remove `r20_backend` `/api/v1/admin/*` + `admin_auth` | **Done** |
| Delete unused dashboard/admin-era scripts | **Done** |
| Hard-delete `r20_gateway/` + remaining gateway/script helpers | **Done** |
| O3 — hard-delete `r20_backend` stubs + `r20-*.service` units | **Done** |

---

## Removed packages / units

| Path | Status |
|------|--------|
| `r20_backend/` (entire package, including former 410/soft-block stubs) | **Deleted** |
| `r20_gateway/` (entire package) | **Deleted** |
| `keel/legacy/` quarantine helpers | **Deleted** (no remaining stub callers) |
| `deploy/r20-quantum.service` | **Deleted** |
| `deploy/r20-scheduler.service` | **Deleted** |
| `deploy/r20-gateway.service` | **Deleted** (earlier) |

Prefer `keel.notify` (Null/Webhook) for notifications.

## Deploy units

Install / enable **only** `keel-*.service`.

| Unit | Default | Notes |
|------|---------|-------|
| `deploy/keel-api.service` | **enable** | Primary API (+ optional `frontend/dist` SPA) |
| `deploy/keel-worker.service` | **enable** | Sole scheduler |

## Scripts

| Path | Status |
|------|--------|
| *(none remaining under `scripts/` besides `run_keel_tests.sh`)* | Product path is `python -m keel.worker` only |

### Deleted scripts / helpers (inventory)

| Path | Reason |
|------|--------|
| `scripts/sync_web_data.py` | R20 dashboard cache generator; dashboard package gone |
| `scripts/daemon_web_sync.py` | Console sync daemon that only drove `sync_web_data` + harvesters |
| `scripts/generate_snapshots.py` | Orphan equity-snapshot CLI |
| `scripts/debug_aggregate_orders.py` | Orphan OKX debug one-shot |
| `scripts/debug_audit_bills.py` | Orphan OKX debug one-shot |
| `scripts/remove_retired_personal_wechat.py` | Spent one-shot WeChat credential migration |
| `scripts/cleanup_disk.py` | Orphan disk cleanup CLI |
| `scripts/calculus_replay.py` | Orphan offline replay CLI |
| `scripts/ai_factor_trader.py` | Retired; product path `python -m keel.worker` |
| `scripts/calculus_engine.py` | Migrated to `keel.factors.kinematics` |
| `scripts/qq_notifier.py` | QQ product non-goal; Keel uses `keel.notify` |
| `scripts/ai_brain_trader.py` | Retired LLM brain loop |
| `scripts/db_manager.py` | Orphan SQLite helper |
| `scripts/factor_library.py` | Legacy scheduler job |
| `scripts/news_sentiment_harvester.py` | Legacy news job |
| `scripts/daily_summary_and_backup.py` | Legacy briefing job |
| `scripts/nightly_backup_and_clean.py` | Legacy nightly backup job |
| `scripts/backup_runtime.py` | Only used by deleted backup scripts |
| `scripts/sync_full_ledger.py` | Only used by deleted briefing/backup scripts |
| `scripts/self_improvement_engine.py` | Legacy evolution job |
| `scripts/instrument_pool.py` | Only used by deleted legacy scripts |
| `scripts/okx_runtime.py` | Legacy CLI/env helper |
| `scripts/prompt_library.py` | Replaced by `keel.llm.prompts` |
| `scripts/r20_okx_setup.py` | Wrapper for deleted `okx_setup` |
| `r20_gateway/` (entire package) | Scripts no longer need gateway; Keel notify is `keel.notify` |
| `r20_backend/` (entire package) | Soft-block stubs retired in O3 |
| `deploy/r20-*.service` | Replaced by keel-api / keel-worker |
| Former `r20_backend` helpers (`config`, `notifications`, `llm_manager`, …) | Deleted with package |
| Gateway / notifications / control-plane tests | Covered deleted modules |

## Removed UI / admin artifacts

| Path | Status |
|------|--------|
| `dashboard/` | **Deleted** |
| Vue `/legacy` + `DashboardView` + R20 `/api/all` shell components | **Deleted** |
| Vue `/admin/*` + `AdminLayout` + `views/admin/**` + auth/`useApi` | **Deleted** |
| `frontend/public/admin/legacy.html` | **Deleted** |
| `r20_backend/admin.html` / `admin_auth.py` / `/api/v1/admin/*` | **Deleted** with package |
| `frontend/` monitor | **Supported** client of `keel.api` (`/`, `/monitor`) |

## Verification

```sh
test ! -d r20_backend
test ! -d r20_gateway
test ! -d keel/legacy
test ! -f deploy/r20-quantum.service
test ! -f deploy/r20-scheduler.service
test ! -f deploy/r20-gateway.service
test ! -d dashboard
test ! -d frontend/src/views/admin
test ! -f scripts/ai_brain_trader.py
make test                                # Keel core tests
PYTHONPATH=. pytest tests/test_keel_*.py tests/test_legacy_quarantine.py -q
```
