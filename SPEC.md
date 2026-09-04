# Keel Trader — Product & Technical Spec (v1)

Status: **Draft for implementation** (2026-09-03)  
Repo: `Golden-Hound-Catcher/keel-trader`  
Audience: engineering + product direction for the elegant refactor

This document is the source of truth for what we build next.  
If `ARCHITECTURE.md` conflicts with this file on *intent*, update ARCHITECTURE to match SPEC.

---

## 1. One-liner

**Keel Trader** is a small, testable execution kernel for OKX USDT-margined swaps:  
**factors → replaceable decision policy → hard risk gates → typed exchange → append-only ledger**, with a **read-only control plane**.

Metaphor: ship’s **keel** — structural core first; hull/UI later.

---

## 2. Goals

| ID | Goal |
|----|------|
| G1 | One scheduler owner only (no double-fire risk) |
| G2 | Exchange access only via typed `Protocol` (no shell `okx` CLI on happy path) |
| G3 | SQLite ledger is the system of record (no JSON-file IPC) |
| G4 | Risk gates cannot be overridden by LLM output |
| G5 | Decision policy is swappable (stub / rules / LLM) |
| G6 | Demo/simulated trading is the default |
| G7 | API is read-only by default (no casual HTTP trade triggers) |
| G8 | Legacy R20 paths are quarantined, then removed in stages |

---

## 3. Non-goals (v1)

- Bloomberg-style terminal clone as a rewrite of R20 marketing UI
- Multi-channel notify expansion (QQ / WeCom / Telegram) as core
- Multi-cloud backup / disaster-recovery productization
- Multi-agent “council” platform theater
- Smart-money crawlers / Top100 scraping as required path
- Plugin marketplace with fake manifests
- Live trading without explicit env `KEEL_OKX_ENV=live` **and** operator confirmation in runbooks
- HTTP endpoints that place orders without a separate, explicitly enabled kill-switch workflow (out of v1)

---

## 4. Runtime topology

Exactly **two** supported long-running processes:

```
keel-api     →  uvicorn keel.api.app:app   (read-only HTTP)
keel-worker  →  python -m keel.worker      (sole scheduler + cycle)
```

Optional one-shot:

```
python -m keel.worker --once
```

Legacy `r20_backend` / `r20_gateway` / `dashboard` are **not** supported entrypoints for new deployments (see `LEGACY.md`).
`frontend/` U1 monitor **is** supported as a read-only client of `keel.api`.
`uvicorn r20_backend.app:app` requires `KEEL_ALLOW_LEGACY_BACKEND=1` or exits pointing at `keel.api`; even with opt-in it is a **410 stub** (admin HTTP API removed).

---

## 5. Package boundaries

| Package | Responsibility | Must not |
|---------|----------------|----------|
| `keel.domain` | Types & invariants | I/O, HTTP, LLM |
| `keel.factors` | Pure factor functions | Network, DB writes |
| `keel.policy` | `DecisionPolicy` Protocol + stub/rule/LLM | Direct order placement |
| `keel.llm` | OpenAI-compatible client + prompt modules | Risk / exchange |
| `keel.risk` | Hard gates | Call LLM |
| `keel.exchange` | `ExchangeProtocol`, Paper, OKX REST | Shell CLI |
| `keel.execution` | Orchestrate decision→risk→place | Own scheduling |
| `keel.ledger` | Append-only SQLite events | Business policy |
| `keel.worker` | Single scheduler + cycle | Second scheduler |
| `keel.api` | Read-only HTTP | Place/cancel orders (v1) |
| `keel.config` | Env-based settings | Multiple secret stores |

---

## 6. Data & control flow

```
worker tick
  → market/account via ExchangeProtocol
  → FactorSnapshot (pure)
  → DecisionPolicy.decide(...) → Decision (JSON-schema validated)
  → RiskGates.evaluate(Decision, portfolio) → allow/deny/resize
  → ExecutionOrchestrator → ExchangeProtocol
  → Ledger.append(events)
api
  → reads ledger (+ live exchange reads for positions/balance when configured)
```

**Iron rules**

1. Only `keel.worker` schedules trading cycles.  
2. No `subprocess` / shell exchange CLI on the happy path.  
3. No new `data/*.json` drop-folder IPC.  
4. Denied decisions are still ledgered (auditability).

---

## 7. HTTP API contract (v1, read-only)

Base: `keel.api.app`

| Method | Path | Purpose |
|--------|------|---------|
| GET | `/health` | Liveness |
| GET | `/ready` | Readiness (DB open, etc.) |
| GET | `/api/v1/status` | Worker/exchange/policy summary |
| GET | `/api/v1/config` | Non-secret config echo |
| GET | `/api/v1/positions` | Open positions |
| GET | `/api/v1/balance` | Account balance snapshot |
| GET | `/api/v1/decisions` | Recent decisions |
| GET | `/api/v1/decisions/latest/{inst_id}` | Latest decision for instrument |
| GET | `/api/v1/trades` | Recent trade/fill events |
| GET | `/api/v1/events` | Raw ledger events |
| GET | `/api/v1/factors/{inst_id}` | Latest factor snapshot |

**Stability**

- Additive fields OK without version bump.  
- Removing/renaming fields requires `/api/v2` or documented migration.  
- OpenAPI via `/docs` is the machine-readable companion to this table.

**Auth (v1)**

- Default: bind to trusted network / localhost; optional shared bearer later (`KEEL_API_TOKEN`) — not required for first demo.

---

## 8. Configuration (minimal)

Prefer `KEEL_*` names. Demo default.

| Variable | Default | Notes |
|----------|---------|-------|
| `KEEL_OKX_ENV` | `demo` | `demo` \| `live` |
| `KEEL_OKX_API_KEY` | empty | With secret+passphrase → OKX adapter |
| `KEEL_OKX_SECRET_KEY` | empty | |
| `KEEL_OKX_PASSPHRASE` | empty | |
| `KEEL_FORCE_PAPER` | `0` | Force `PaperExchange` |
| `KEEL_POLICY` | `stub` | `stub` \| `rule` \| `llm` |
| `KEEL_LLM_BASE_URL` | — | OpenAI-compatible |
| `KEEL_LLM_API_KEY` | — | |
| `KEEL_LLM_MODEL` | — | |
| `KEEL_LEDGER_DB` | local path | SQLite file |
| `KEEL_USE_LEGACY` | unset | Opt into legacy R20 scripts |

Secrets live in `.env` (`chmod 600`) or process env only. **Do not** add parallel encrypted secret stores in v1.

---

## 9. Decision & risk semantics

### Decision (LLM / policy output)

Must validate against schema (action, instrument, size/notion, optional TP/SL intents, rationale). Invalid → ledger `invalid` / skip execution.

### Risk gates (examples; configurable)

- Kill switch  
- Max notional / contracts per instrument  
- Cooldown after stop / deny  
- Daily loss circuit breaker  

LLM cannot bypass gates.

---

## 10. UI strategy (phased — reuse, don’t graft)

### Phase U0 (now)

- Swagger `/docs` only for Keel API.

### Phase U1 (next UI work)

- **Reuse R20 Vue shell** (layout/components) where useful.  
- **Rebind data layer** to Keel `/api/v1/*` only.  
- **Forbidden:** reading `data/*.json` or calling legacy `r20_backend` private routes from the new UI path.

### Phase U2 (**done**)

- Dropped Jinja `dashboard/` (unmounted from `r20_backend.app`; package removed).  
- Vue monitor at `/` + `/monitor` is the supported read-only UI; `/legacy` route removed.  
- Admin mutations (prompt edit, keys) only through explicit Keel admin APIs (future spec addendum).

### Admin UI + admin API removal (**done**)

- Removed R20 Vue `/admin/*` product surface (`AdminLayout`, `views/admin/**`, auth/`useApi`).  
- Removed `frontend/public/admin/legacy.html` and `r20_backend/admin.html`.  
- Removed `r20_backend.admin_auth` and all `/api/v1/admin/*` FastAPI routes.  
- Soft-blocked `r20_backend.app` is a **410 stub** only (no admin HTTP surface).  
- **Do not** build a full Keel admin in the monitor SPA — deferred to a SPEC addendum.

UI is a **client of the SPEC API**, not a second source of truth.

---

## 11. Legacy R20 retirement plan

| Stage | Action |
|-------|--------|
| Done | Shim traders → Keel cycle; kill dual schedulers; quarantine warnings; `LEGACY.md` |
| Done | Phase U1 monitor rebind to Keel `/api/v1` + `/health` |
| Done | Stop documenting `r20_backend.app` as runnable; soft-block via `KEEL_ALLOW_LEGACY_BACKEND=1` |
| Done | Phase U2 — drop Jinja `dashboard/`; remove `/legacy` Vue route |
| Done | Remove Vue `/admin/*` product surface; no HTML admin UI |
| Done | Remove `r20_backend` `/api/v1/admin/*` + `admin_auth` (410 stub) |
| Done | Delete unused dashboard/admin-era scripts; hard-gate `GatewayScheduler.tick` |
| Last | Remove remaining `r20_backend` helpers once gateway/scripts no longer need them |

Supported deployments: **keel-api** + **keel-worker** + optional **frontend** U1 monitor only.
No mass-delete without inventory check against `LEGACY.md`.

---

## 12. Testing & quality bar

- `pytest tests/test_keel_*.py` (+ quarantine tests) must stay green before merge.  
- Domain/factors/risk/policy unit-tested offline.  
- OKX adapter tests use injectable/mock HTTP (no live network required in CI).  
- Manual demo trial: `.env` with demo keys → `python -m keel.worker --once` → verify ledger + `/api/v1/*`.

---

## 13. Demo trial acceptance (operator)

1. `KEEL_OKX_ENV=demo` + three demo credentials in `.env`  
2. `build_exchange()` selects OKX REST (not paper)  
3. One worker cycle completes without shell CLI  
4. Ledger contains decisions/events; API returns them  
5. Default policy may be `stub`/`rule` for connectivity; LLM optional

---

## 14. Open decisions (resolve in addenda)

| ID | Question | Default until decided |
|----|----------|------------------------|
| O1 | Vue reuse depth (full admin vs monitor-only) | **Monitor-only** (R20 `/admin` removed; Keel admin = future addendum) |
| O2 | API auth for non-local binds | None in v1 local/demo |
| O3 | When to hard-delete `r20_*` packages | After gateway/scripts stop needing helpers (admin API already gone) |

---

## 15. Implementation order (post-spec)

1. Freeze this SPEC on `main` — **done**  
2. Demo trial (keys in local `.env`, not chat)  
3. Phase U1 UI rebind to Keel API — **done**  
4. Retire `r20_backend.app` as a documented/runnable entry (soft-block) — **done**  
5. Phase U2 drop Jinja `dashboard/` — **done**  
6. Remove Vue `/admin` product surface — **done**
7. Remove `r20_backend` `/api/v1/admin/*` + `admin_auth` — **done**
8. Continue legacy deletion behind inventory (remaining helpers / gateway / scripts)  

---

## 16. Changelog

| Date | Note |
|------|------|
| 2026-09-03 | Initial SPEC v1 drafted after stages 2–7 refactor |
| 2026-09-03 | §11: U1 done; soft-block `r20_backend.app`; supported = keel-api + keel-worker + U1 UI |
| 2026-09-03 | §10/§11: U2 done — Jinja `dashboard/` removed; `/legacy` gone; `r20_backend` admin-only remnant |
| 2026-09-03 | Removed Vue `/admin/*` product surface; admin features deferred to future Keel admin API |
| 2026-09-03 | Removed `r20_backend` `/api/v1/admin/*` + `admin_auth`; stub returns 410 |
| 2026-09-03 | Elegance: Pydantic API schemas; domain owns Decision/records; kinematics in keel.factors; calculus_engine shim |
| 2026-09-04 | Inventory-gated delete: `r20_backend/okx_client.py`, `prompt_views.py` (0 refs); kept `qq_gateway_daemon`/`audit` (qq_bind) |

---

## Addendum: Phase U1 delivered

Default O1 = **monitor-only**. Vue shell reused for layout/theme; data layer rebound to Keel:

| Monitor data | Endpoint |
|--------------|----------|
| connectivity | `GET /health` |
| status | `GET /api/v1/status` |
| balance / positions | `GET /api/v1/balance`, `/api/v1/positions` |
| decisions / trades / events | `GET /api/v1/decisions`, `/trades`, `/events` |
| factors | `GET /api/v1/factors/{inst_id}` |

Primary UI route: `/` (`MonitorView`). Jinja dashboard, `/legacy`, and R20 `/admin/*` are removed. Admin features deferred to a future Keel admin API (SPEC addendum). Vite proxies `/api` + `/health` to `:8080`. See `frontend/README.md`.

---

## Addendum: Phase U2 delivered

- Deleted `dashboard/` (Jinja templates, static JS, `app.py`, start/stop scripts).
- `r20_backend.app` no longer imports or mounts `dashboard`; soft-block kept for rare API remnant use.
- Frontend: removed `/legacy` + `DashboardView` and R20 shell components bound to `/api/all`.
- Supported UI remains `/` + `/monitor` on Keel `/health` + `/api/v1/*`.

## Addendum: Legacy `/admin` UI removed

- Deleted Vue `views/admin/**`, `AdminLayout.vue`, admin router/nav, `stores/auth.ts`, `composables/useApi.ts`.
- Deleted `frontend/public/admin/legacy.html` and `r20_backend/admin.html`.
- Soft-blocked `r20_backend.app` `/admin` HTML route returned **410** (prior PR).
- Supported product UI: Keel monitor only (`/`, `/monitor`).

## Addendum: Legacy `/api/v1/admin/*` removed

- Deleted `r20_backend/admin_auth.py` and all `/api/v1/admin/*` FastAPI routes from `r20_backend/app.py`.
- `r20_backend.app` is now a soft-blocked **410 stub** (any path → Gone; prefer `keel.api`).
- Retired admin-coupled tests (`test_admin_*`, `test_control_plane_v2_api`, `test_custom_system_api`); kept `llm_manager` unit coverage without HTTP.
- Remaining `r20_backend` modules exist for optional `r20_gateway` / historical `scripts/` only — not for product admin.

## Addendum: Unused legacy scripts / gateway tick gate

- Deleted orphan dashboard/admin-era scripts: `sync_web_data`, `daemon_web_sync`,
  `generate_snapshots`, `debug_aggregate_orders`, `debug_audit_bills`,
  `remove_retired_personal_wechat`, `cleanup_disk`, `calculus_replay`.
- Kept trader shims (`ai_factor_trader`, `ai_brain_trader`) and scripts still
  launched by `keel.worker` (factor/news/briefing/backup/self-improvement helpers).
- `r20_gateway.worker` remains notify-only by default; `GatewayScheduler.tick()` /
  `_execute` are no-ops unless `KEEL_ENABLE_LEGACY_GATEWAY_SCHEDULER=1`.
- Remaining `r20_backend` helpers stay until gateway/scripts no longer need them.

## Addendum: Typed API + domain / factors elegance

- Introduced `keel.api.schemas` Pydantic models for health/status/positions/balance/decisions/trades/events/factors.
- Moved ledger record dataclasses and `Decision` into `keel.domain` (llm re-exports Decision).
- Ported `scripts/calculus_engine` pure math into `keel.factors.kinematics` (honest names); scripts module is a deprecated shim for legacy imports.

## Addendum: Dead r20_backend helpers removed (inventory)

- Deleted `r20_backend/okx_client.py` and `r20_backend/prompt_views.py` after repo-wide
  reference scan showed **zero** external imports/usages (admin UI already gone; Keel
  OKX REST lives under `keel`).
- **Kept** `r20_backend/qq_gateway_daemon.py` (spawned by `qq_bind.ensure_qq_gateway_daemon_running`)
  and `r20_backend/audit.py` (imported by the daemon).
- No dedicated unit tests existed solely for the deleted modules.
