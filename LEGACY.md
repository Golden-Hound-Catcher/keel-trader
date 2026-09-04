# Legacy inventory (quarantine after gateway / helper hard-delete)

Keel Trader keeps soft-blocked R20 **stubs** only. **The Vue `/admin/*` UI, Jinja
`dashboard/`, Vue `/legacy`, `admin_auth`, `/api/v1/admin/*` HTTP routes, the
entire `r20_gateway/` package, and remaining gateway/script helper modules are
gone.** `r20_backend.app` is a soft-blocked **410 stub**; `r20_backend.scheduler`
hard-exits. **New deployments must not use legacy entrypoints.**

## Supported runtime (only)

| Process | Module / unit |
|---------|----------------|
| API | `uvicorn keel.api.app:app` / `deploy/keel-api.service` |
| Scheduler | `python -m keel.worker` / `deploy/keel-worker.service` |
| Monitor UI | `frontend/` Vite app -> Keel `/health` + `/api/v1/*` (routes `/`, `/monitor`) |

Opt-in flags:

| Env | Purpose |
|-----|---------|
| `KEEL_USE_LEGACY=1` | Acknowledge legacy stubs; silence import quarantine warnings |
| `KEEL_ALLOW_LEGACY_BACKEND=1` | **Required** to import/serve `r20_backend.app` (410 stub only) |

`KEEL_ENABLE_LEGACY_GATEWAY_SCHEDULER` is obsolete — `r20_gateway` was removed.
Keel scheduler is **trader-only** (`python -m keel.worker`).

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
| Delete unused dashboard/admin-era scripts; hard-gate gateway job launch | **Done** |
| Hard-delete `r20_gateway/` + remaining gateway/script helpers | **Done** |

---

## Packages (kept stubs only)

| Path | Why it remains | Accidental-use guard |
|------|----------------|----------------------|
| `r20_backend/__init__.py` | Package root for stubs | Import warn; prefer `keel.api` |
| `r20_backend/app.py` | Soft-blocked **410 stub** (admin HTTP API removed) | **`KEEL_ALLOW_LEGACY_BACKEND=1` required** to import via uvicorn; else exit `2` |
| `r20_backend/scheduler.py` | Hard guard against double-firing | Immediate exit code `2` |

### Removed from `r20_backend`

| Path | Status |
|------|--------|
| `r20_backend/admin_auth.py` | **Deleted** |
| `r20_backend/app.py` admin FastAPI routes (`/api/v1/admin/*`, HTML `/admin`) | **Deleted** (replaced by 410 stub) |
| `r20_backend/admin.html` | **Deleted** (prior PR) |
| `r20_backend/okx_client.py` | **Deleted** |
| `r20_backend/prompt_views.py` | **Deleted** |
| `r20_backend/account_baseline.py` | **Deleted** |
| `r20_backend/okx_trade_service.py` | **Deleted** |
| `r20_backend/okx_setup.py` | **Deleted** |
| `r20_backend/qq_bind.py` | **Deleted** |
| `r20_backend/qq_gateway_daemon.py` | **Deleted** |
| `r20_backend/audit.py` | **Deleted** |
| `r20_backend/council_manager.py` | **Deleted** |
| `r20_backend/config.py` | **Deleted** (0 importers after script/gateway delete) |
| `r20_backend/backup_secrets.py` | **Deleted** |
| `r20_backend/backup_store.py` | **Deleted** |
| `r20_backend/llm_manager.py` | **Deleted** |
| `r20_backend/settings_store.py` | **Deleted** |
| `r20_backend/notifications.py` | **Deleted** (Keel uses `keel.notify`) |
| `r20_backend/schedule_store.py` | **Deleted** |
| `r20_backend/net_security.py` | **Deleted** |

### Removed package: `r20_gateway/`

Entire directory **deleted** (worker, scheduler, store, publisher, channels,
events, secrets, telemetry, `__init__`). Deploy unit `deploy/r20-gateway.service`
removed. Prefer `keel.notify` (Null/Webhook).

## Deploy units

Install examples / enable only `keel-*.service`. Remaining `r20-*.service` stay
disabled/gated.

| Unit | Default | Notes |
|------|---------|-------|
| `deploy/keel-api.service` | **enable** | Primary API (+ optional `frontend/dist` SPA) |
| `deploy/keel-worker.service` | **enable** | Sole scheduler |
| `deploy/r20-quantum.service` | **disabled** | Gated; would serve the 410 stub only — prefer keel-api |
| `deploy/r20-gateway.service` | **deleted** | Package removed |
| `deploy/r20-scheduler.service` | **disabled** | `ConditionPathExists` + `/bin/false` (aligns with soft-blocked backend scheduler) |

## Scripts (shim or historical)

| Path | Status |
|------|--------|
| *(none remaining under `scripts/` besides `run_keel_tests.sh`)* | Product path is `python -m keel.worker` only |

### Deleted scripts / gateway helpers (inventory)

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
| `deploy/r20-gateway.service` | Package removed |
| `r20_backend/config.py` | 0 importers after script/gateway delete |
| `r20_backend/backup_secrets.py` | Gateway/scripts-only |
| `r20_backend/backup_store.py` | Gateway/scripts-only |
| `r20_backend/llm_manager.py` | Gateway/scripts/admin-only |
| `r20_backend/settings_store.py` | Only served `llm_manager` |
| `r20_backend/notifications.py` | Only served gateway channels |
| `r20_backend/schedule_store.py` | Only served gateway scheduler |
| `r20_backend/net_security.py` | Only served notifications/backup |
| `tests/test_gateway.py` | Covered deleted gateway |
| `tests/test_gateway_runtime.py` | Covered deleted gateway |
| `tests/test_gateway_scheduler.py` | Covered deleted gateway scheduler |
| `tests/test_notifications.py` | Covered deleted notifications |
| `tests/test_llm_multi_provider.py` | Covered deleted llm_manager |
| `tests/test_control_plane_v2.py` | Covered deleted notifications/gateway store |
| `tests/test_open_source_control.py` | Covered deleted notifications/backup/net_security |

## Removed UI / admin artifacts

| Path | Status |
|------|--------|
| `dashboard/` | **Deleted** |
| Vue `/legacy` + `DashboardView` + R20 `/api/all` shell components | **Deleted** |
| Vue `/admin/*` + `AdminLayout` + `views/admin/**` + auth/`useApi` | **Deleted** |
| `frontend/public/admin/legacy.html` | **Deleted** |
| `r20_backend/admin.html` | **Deleted** |
| `r20_backend/admin_auth.py` | **Deleted** |
| `/api/v1/admin/*` HTTP routes | **Deleted** (stub returns 410) |
| `frontend/` monitor | **Supported** client of `keel.api` (`/`, `/monitor`) |

## Verification

```sh
python -m r20_backend.scheduler          # must exit 2
python -c "import r20_backend.app"       # must exit 2 without KEEL_ALLOW_LEGACY_BACKEND=1
KEEL_ALLOW_LEGACY_BACKEND=1 python -c "import r20_backend.app"  # opt-in ok (410 stub only)
test ! -d r20_gateway
test ! -f deploy/r20-gateway.service
test ! -f r20_backend/config.py
test ! -f r20_backend/notifications.py
test ! -f r20_backend/llm_manager.py
test ! -f r20_backend/admin_auth.py
test ! -f scripts/ai_brain_trader.py
test ! -d dashboard
test ! -d frontend/src/views/admin
make test                                # Keel core tests
PYTHONPATH=. pytest tests/test_keel_*.py tests/test_legacy_quarantine.py -q
```
