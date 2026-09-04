# Legacy inventory (quarantine after unused-script / gateway trim)

Keel Trader keeps historical R20 helper modules in-tree for soft-blocked
gateway/backend rollback. **The Vue `/admin/*` UI, Jinja `dashboard/`, Vue
`/legacy`, `admin_auth`, and `/api/v1/admin/*` HTTP routes are gone.**
Unused dashboard/admin-era scripts (`sync_web_data`, `daemon_web_sync`, debug
one-shots, etc.) are **deleted**.
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
| `KEEL_ENABLE_LEGACY_GATEWAY_SCHEDULER=1` | Emergency: re-enable gateway job ticks (worker + `GatewayScheduler.tick`) |

Gateway job ticks remain separately gated (notify-only by default). Prefer Keel units only.
Keel scheduler is **trader-only** (`python -m keel.worker`); legacy script JobSpecs are deleted.

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
| Remove remaining `r20_backend` / `r20_gateway` helpers | Later (inventory-gated; still used by gateway/scripts) |

---

## Packages (kept, hard to misuse)

| Path | Why it remains | Accidental-use guard |
|------|----------------|----------------------|
| `r20_backend/` | Helper modules for optional gateway/scripts (notifications, llm_manager, backup_*, settings_store, etc.) | Import warn; prefer `keel.api` |
| `r20_backend/app.py` | Soft-blocked **410 stub** (admin HTTP API removed) | **`KEEL_ALLOW_LEGACY_BACKEND=1` required** to import via uvicorn; else exit `2` |
| `r20_backend/scheduler.py` | Hard guard against double-firing | Immediate exit code `2` |
| `r20_gateway/` | Optional notification delivery | Import warn; no job ticks by default |
| `r20_gateway/worker.py` | Notify-only loop | Loud stderr warn on `run()`; scheduler off |
| `r20_gateway/scheduler.py` | Timing helpers + emergency rollback | `tick()` / `_execute` no-op unless `KEEL_ENABLE_LEGACY_GATEWAY_SCHEDULER=1` |

### Removed from `r20_backend`

| Path | Status |
|------|--------|
| `r20_backend/admin_auth.py` | **Deleted** |
| `r20_backend/app.py` admin FastAPI routes (`/api/v1/admin/*`, HTML `/admin`) | **Deleted** (replaced by 410 stub) |
| `r20_backend/admin.html` | **Deleted** (prior PR) |
| `r20_backend/okx_client.py` | **Deleted** (0 external refs; Keel uses `keel` OKX REST) |
| `r20_backend/prompt_views.py` | **Deleted** (0 external refs; admin prompt editor gone) |
| `r20_backend/account_baseline.py` | **Deleted** (0 external refs; mutual self/test only) |
| `r20_backend/okx_trade_service.py` | **Deleted** (0 Keel/gateway/scripts refs; tests-only) |
| `r20_backend/okx_setup.py` | **Deleted** (0 Keel importers; Keel uses `keel.exchange` OKX REST + `KEEL_OKX_*`) |
| `r20_backend/qq_bind.py` | **Deleted** (QQ product non-goal; 0 Keel importers) |
| `r20_backend/qq_gateway_daemon.py` | **Deleted** (only spawned by `qq_bind`) |
| `r20_backend/audit.py` | **Deleted** (only imported by `qq_gateway_daemon`) |
| `r20_backend/council_manager.py` | **Deleted** (multi-agent council non-goal; only `ai_brain_trader` + its test) |

Keel code does **not** import these helpers; they remain for `r20_gateway` and remaining historical `scripts/` only.
Keel notify uses `keel.notify` (Null/Webhook) instead of the removed QQ bind/daemon stack and deleted `scripts/qq_notifier.py` bridge.

### Removed from `r20_gateway`

| Path | Status |
|------|--------|
| `r20_gateway/agents.py` | **Deleted** (0 refs; admin-era agent registry) |
| `r20_gateway/supervisor.py` | **Deleted** (0 refs; unused worker supervisor wrapper) |
| `r20_gateway/plugins.py` | **Deleted** (fake plugin manifests; only referenced by control-plane tests) |

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
| *(none remaining under `scripts/` besides `run_keel_tests.sh`)* | Product path is `python -m keel.worker` only |

### Deleted scripts / gateway helpers (inventory)

| Path | Reason |
|------|--------|
| `scripts/sync_web_data.py` | R20 dashboard cache generator; dashboard package gone; nothing imported it for Keel |
| `scripts/daemon_web_sync.py` | Console sync daemon that only drove `sync_web_data` + harvesters |
| `scripts/generate_snapshots.py` | Orphan equity-snapshot CLI; unused by Keel/tests |
| `scripts/debug_aggregate_orders.py` | Orphan OKX debug one-shot |
| `scripts/debug_audit_bills.py` | Orphan OKX debug one-shot |
| `scripts/remove_retired_personal_wechat.py` | Spent one-shot WeChat credential migration |
| `scripts/cleanup_disk.py` | Orphan disk cleanup CLI; unused by nightly/Keel |
| `scripts/calculus_replay.py` | Orphan offline replay CLI |
| `scripts/ai_factor_trader.py` | Retired OKX-CLI / shim; product path is `python -m keel.worker` / `keel.worker.cycle` |
| `scripts/calculus_engine.py` | Deprecated shim; importers migrated to `keel.factors.kinematics` |
| `scripts/qq_notifier.py` | QQ product non-goal; call sites removed; Keel uses `keel.notify` |
| `scripts/ai_brain_trader.py` | Retired LLM brain loop (council already gone); product path is `python -m keel.worker` |
| `scripts/db_manager.py` | Orphan SQLite helper; zero refs after `ai_factor_trader` drop |
| `scripts/factor_library.py` | Legacy KeelScheduler/gateway job; product path `python -m keel.worker` |
| `scripts/news_sentiment_harvester.py` | Legacy news job removed with scheduler script map |
| `scripts/daily_summary_and_backup.py` | Legacy briefing job removed |
| `scripts/nightly_backup_and_clean.py` | Legacy nightly backup job removed |
| `scripts/backup_runtime.py` | Only used by deleted nightly/briefing backup scripts |
| `scripts/sync_full_ledger.py` | Only used by deleted briefing/backup scripts |
| `scripts/self_improvement_engine.py` | Legacy evolution job + tests removed |
| `scripts/instrument_pool.py` | Only used by deleted legacy scripts |
| `scripts/okx_runtime.py` | Legacy CLI/env helper; `r20_backend.config` inlined selection |
| `scripts/prompt_library.py` | Replaced by `keel.llm.prompts`; comment updated in compose.py |
| `tests/test_prompt_library.py` | Covered deleted `prompt_library` |
| `tests/test_backup_methods.py` | Covered deleted `backup_runtime` |
| `tests/test_prompt_math_foundations.py` | Covered deleted `self_improvement_engine` |
| `tests/test_custom_systems.py` | Covered deleted prompt/backup script stack |
| `r20_gateway/agents.py` | 0 external refs (admin-era agent registry) |
| `r20_gateway/supervisor.py` | 0 external refs (unused supervisor wrapper) |
| `r20_backend/qq_bind.py` | QQ product non-goal; 0 Keel/scripts importers |
| `r20_backend/qq_gateway_daemon.py` | Only spawned by deleted `qq_bind` |
| `r20_backend/audit.py` | Only imported by deleted `qq_gateway_daemon` |
| `r20_gateway/plugins.py` | Plugin marketplace non-goal; tests-only refs |
| `tests/test_qq_bind.py` | Covered deleted QQ bind module |
| `tests/test_council_manager.py` | Covered deleted `council_manager` |
| `scripts/r20_okx_setup.py` | CLI wrapper for deleted `okx_setup`; install.sh now points at `KEEL_OKX_*` |
| `tests/test_okx_setup.py` | Covered deleted `okx_setup` |

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
test ! -f r20_backend/okx_client.py
test ! -f r20_backend/prompt_views.py
test ! -f r20_backend/account_baseline.py
test ! -f r20_backend/okx_trade_service.py
test ! -f r20_backend/okx_setup.py
test ! -f scripts/r20_okx_setup.py
test ! -f scripts/ai_brain_trader.py
test ! -f scripts/ai_factor_trader.py
test ! -f scripts/db_manager.py
test ! -f scripts/factor_library.py
test ! -f scripts/news_sentiment_harvester.py
test ! -f scripts/daily_summary_and_backup.py
test ! -f scripts/nightly_backup_and_clean.py
test ! -f scripts/backup_runtime.py
test ! -f scripts/sync_full_ledger.py
test ! -f scripts/self_improvement_engine.py
test ! -f scripts/instrument_pool.py
test ! -f scripts/okx_runtime.py
test ! -f scripts/prompt_library.py
test ! -f tests/test_prompt_library.py
test ! -f tests/test_backup_methods.py
test ! -f tests/test_prompt_math_foundations.py
test ! -f tests/test_custom_systems.py
test ! -f tests/test_okx_setup.py
test ! -f r20_backend/qq_bind.py
test ! -f r20_backend/qq_gateway_daemon.py
test ! -f r20_backend/audit.py
test ! -f r20_backend/council_manager.py
test ! -f r20_gateway/plugins.py
test ! -f tests/test_qq_bind.py
test ! -f tests/test_council_manager.py
test ! -d dashboard
test ! -d frontend/src/views/admin
test ! -f scripts/sync_web_data.py
test ! -f scripts/daemon_web_sync.py
test ! -f scripts/calculus_engine.py
test ! -f r20_gateway/agents.py
test ! -f r20_gateway/supervisor.py
make test                                # Keel core tests
python -m pytest tests/test_legacy_quarantine.py tests/test_gateway_scheduler.py -v
```
