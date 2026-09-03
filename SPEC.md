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

Legacy `r20_backend` / `r20_gateway` / `dashboard` / `frontend` are **not** supported entrypoints for new deployments (see `LEGACY.md`).

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

### Phase U2

- Drop Jinja `dashboard/` once Vue+Keel parity for read-only monitoring exists.  
- Admin mutations (prompt edit, keys) only through explicit Keel admin APIs (future spec addendum).

UI is a **client of the SPEC API**, not a second source of truth.

---

## 11. Legacy R20 retirement plan

| Stage | Action |
|-------|--------|
| Done | Shim traders → Keel cycle; kill dual schedulers; quarantine warnings; `LEGACY.md` |
| Next | UI rebind (U1); stop documenting `r20_backend.app` as runnable |
| Later | Delete `dashboard/`, unused scripts, `r20_gateway` job scheduler code |
| Last | Remove `r20_backend` once no mounts remain |

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
| O1 | Vue reuse depth (full admin vs monitor-only) | Monitor-only U1 first |
| O2 | API auth for non-local binds | None in v1 local/demo |
| O3 | When to hard-delete `r20_*` packages | After U1 green |

---

## 15. Implementation order (post-spec)

1. Freeze this SPEC on `main`  
2. Demo trial (keys in local `.env`, not chat)  
3. Phase U1 UI rebind to Keel API  
4. Continue legacy deletion behind inventory  

---

## 16. Changelog

| Date | Note |
|------|------|
| 2026-09-03 | Initial SPEC v1 drafted after stages 2–7 refactor |

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

Primary UI route: `/` (`MonitorView`). Legacy R20 dashboard at `/legacy`; admin under `/admin/*` (labeled legacy). Vite proxies `/api` + `/health` to `:8080`. See `frontend/README.md`.
