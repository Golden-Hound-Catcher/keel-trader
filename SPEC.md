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

Legacy `r20_*` packages / `dashboard` are **removed** and are **not** supported entrypoints (see `LEGACY.md`).
`frontend/` U1 monitor **is** supported as a read-only client of `keel.api`.
Supported processes: `uvicorn keel.api.app:app` + `python -m keel.worker` (+ optional Vite).

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
| `keel.notify` | Optional `Notifier` port (Null / Webhook) | QQ/WeCom/Telegram product expansion; deleted r20_gateway |

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
  → optional Notifier.notify(cycle summary)  # Null if KEEL_NOTIFY_WEBHOOK_URL empty
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
| GET | `/ready` | Readiness: ledger open + `seconds_since_last_cycle` / `worker_stale` (stale when lag > max(2×cycle_interval, cycle_interval+300); null lag = cold-start OK) |
| GET | `/api/v1/status` | Worker/exchange/policy summary; `decision_policy`; optional `last_cycle`; `seconds_since_last_cycle`; `worker_stale` (same threshold as `/ready`) |
| GET | `/api/v1/config` | Non-secret config echo (incl. instruments from `KEEL_INSTRUMENTS`, `decision_policy`, exchange_mode, notify_configured / alerts_only / format, `cycle_interval_seconds`, `scheduler_jobs`) |
| GET | `/api/v1/pnl/daily` | Realized daily PnL from ledger (Beijing date; optional `?date=YYYY-MM-DD`) |
| GET | `/api/v1/positions` | Open positions |
| GET | `/api/v1/balance` | Account balance snapshot |
| GET | `/api/v1/decisions` | Recent decisions (`?inst_id=` optional) |
| GET | `/api/v1/decisions/latest/{inst_id}` | Latest decision for instrument |
| GET | `/api/v1/trades` | Recent trade/fill events (`?inst_id=` optional) |
| GET | `/api/v1/events` | Raw ledger events (`?event_type=` / `?inst_id=` optional) |
| GET | `/api/v1/factors/{inst_id}` | Latest factor snapshot (`?live=1` → OKX public candles) |
| GET | `/api/v1/stats/decisions` | Decision quality aggregates (`?hours=`, optional `market_source=okx_public|synthetic|any`) |
| GET | `/api/v1/stats/shadow` | Shadow fill / probe / skip / markout aggregates (`?hours=`); optional `by_instrument` (counts + compact 300s net-RT) |
| GET | `/api/v1/stats/shadow_markout` | Sibling alias of `/stats/shadow` (same markout payload) |
| GET | `/api/v1/stats/quality` | Observation quality scorecard (`?hours=`): market_source breakdown, wait/near_signal rates, shadow nest, cycle timing, optional `by_instrument` |
| GET | `/api/v1/signals/nearest` | Q0 near-signal radar: latest decision per watch instrument + `signal_diag` summary (`?hours=`) |

**Stability**

- Additive fields OK without version bump.  
- Removing/renaming fields requires `/api/v2` or documented migration.  
- OpenAPI via `/docs` is the machine-readable companion to this table.

**Auth (v1)**

- Default: bind to trusted network / localhost; optional shared bearer (`KEEL_API_TOKEN`) for non-local binds — empty token keeps v1 local/demo open.

---

## 8. Configuration (minimal)

Prefer `KEEL_*` names. Demo default.

| Variable | Default | Notes |
|----------|---------|-------|
| `KEEL_OKX_ENV` | `demo` | `demo` | `live` |
| `KEEL_OKX_API_KEY` | empty | With secret+passphrase → OKX adapter |
| `KEEL_OKX_SECRET_KEY` | empty | |
| `KEEL_OKX_PASSPHRASE` | empty | |
| `KEEL_FORCE_PAPER` | `0` | Force `PaperExchange` |
| `KEEL_POLICY` | `stub` | `stub` | `rule` | `llm` |
| `KEEL_LLM_BASE_URL` | — | OpenAI-compatible |
| `KEEL_LLM_API_KEY` | — | |
| `KEEL_LLM_MODEL` | — | |
| `KEEL_LEDGER_DB` | local path | SQLite file |
| `KEEL_KILL_SWITCH` | `0` | `0` | `1` / true\|false — deny all trading when on |
| `KEEL_SHADOW_MODE` | `0` | `0` | `1` — ledger shadow_fill instead of place_order; kill blocks real orders only |
| `KEEL_SHADOW_NEAR_PROBE` | `0` | `0` | `1` — Q3: convert strong WAIT near-signals to shadow_fill when kill+shadow on; never live |
| `KEEL_SHADOW_NEAR_PROBE_COOLDOWN_SECONDS` | `900` | Per-instrument cooldown between probe shadow fills |
| `KEEL_SHADOW_NEAR_PROBE_MAX_MISSING` | `2` | Max `signal_diag.missing` length (aligned with near-signal notify) |
| `KEEL_SHADOW_NEAR_PROBE_MIN_CONFIDENCE` | `0` | Optional min decision confidence gate for probe |
| `KEEL_SHADOW_NEAR_PROBE_MIN_EDGE_BPS` | _(unset)_ | Q3.4: optional explicit edge hurdle (bps); unset → RT/open fee for role |
| `KEEL_SHADOW_NEAR_PROBE_EDGE_MODE` | `round_trip` | `round_trip` | `open` — which fee leg is the near-probe hurdle |
| `KEEL_SHADOW_FEE_ROLE` | `taker` | `taker` | `maker` — fee leg for net markout + near-probe hurdle (shadow assumes immediate fill) |
| `KEEL_SHADOW_MAKER_FEE_BPS` | _(unset)_ | Optional override maker bps (positive=cost, negative=rebate) → source=override |
| `KEEL_SHADOW_TAKER_FEE_BPS` | _(unset)_ | Optional override taker bps → source=override |
| `KEEL_MAX_NOTIONAL_PER_INSTRUMENT` | `2000` | USDT; existing + requested notional (margin×leverage); on `/config` |
| `KEEL_MAX_CONTRACTS_PER_INSTRUMENT` | `50` | Contract/size units per instrument when size known; on `/config` |
| `KEEL_INSTRUMENTS` | empty → defaults | Comma-separated OKX swap ids; empty → `DEFAULT_CRYPTO_INSTRUMENTS`; worker + `/config` instruments |
| `KEEL_DECISION_POLICY` | `rule` | `rule` | `stub` | `llm` — exposed as `decision_policy` on `/status` + `/config` |
| `KEEL_CYCLE_INTERVAL_SECONDS` | `900` | Trader cycle interval; clamped `[60, 86400]`; wins over observe preset when set |
| `KEEL_OBSERVE_PRESET` | unset | Q0 cadence: `default`=900 / `fast`=300 / `slow`=1800; exposed as `observe_preset` on `/config` |
| `KEEL_API_TOKEN` | empty | Optional bearer for `/api/v1/*`; empty → no auth |
| `KEEL_NOTIFY_WEBHOOK_URL` | empty | Empty → NullNotifier (no network); else POST cycle summary |
| `KEEL_NOTIFY_ALERTS_ONLY` | `0` | When true, skip notify unless payload `alert` (deny/error **or** near-signal / BUY|SELL) |
| `KEEL_NOTIFY_FORMAT` | `keel` | `keel` `{"event","payload"}` | `discord` `{"content": text}` ≤1900 |

Secrets live in `.env` (`chmod 600`) or process env only. **Do not** add parallel encrypted secret stores in v1.

---

## 9. Decision & risk semantics

### Decision (LLM / policy output)

Must validate against schema (action, instrument, size/notion, optional TP/SL intents, rationale). Invalid → ledger `invalid` / skip execution.

### Risk gates (examples; configurable)

- Kill switch (`KEEL_KILL_SWITCH`) — when armed, gates deny all trading actions; `WAIT` still ok  
- Shadow mode (`KEEL_SHADOW_MODE`) — when on, risk-pass decisions ledger `shadow_fill` (+ optional synthetic trade marked shadow) and **skip** exchange `place_order`; kill-switch blocks real orders only (shadow may proceed); default off  
- First-live caps (`KEEL_LIVE_MAX_NOTIONAL_PER_INSTRUMENT` default 200, `KEEL_LIVE_MAX_CONTRACTS_PER_INSTRUMENT` default 5) — applied when `okx_environment=live` and not shadow; paper/shadow keep `KEEL_MAX_*`  
- Max notional / contracts per instrument (`KEEL_MAX_NOTIONAL_PER_INSTRUMENT` default 2000 USDT ≈ margin 600×~3.3 lev; `KEEL_MAX_CONTRACTS_PER_INSTRUMENT` default 50) — `MaxNotionalGate` after DailyLoss; closes still ok  
- Cooldown after stop / deny  
- Daily loss circuit breaker  

LLM cannot bypass gates. Exposed as non-secret `kill_switch`, `shadow_mode`, `max_notional_per_instrument`, and `max_contracts_per_instrument` on `GET /api/v1/config` (`kill_switch` / `shadow_mode` also on status).

Active decision policy name (`build_decision_policy` → `describe_policy`, no cycle run) is exposed as `decision_policy` on the same endpoints (and on the monitor Config strip).

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
| Done | Hard-delete `r20_gateway/` + remaining gateway/script helpers |
| Done | O3 hard-delete `r20_backend/` stubs + `r20-*.service` units + `keel/legacy` |

Supported deployments: **keel-api** + **keel-worker** + optional **frontend** U1 monitor only.
`KeelScheduler` jobs: **trader only** (`python -m keel.worker`). Legacy R20 script
jobs and their `scripts/*.py` implementations are deleted (see LEGACY.md inventory).
No mass-delete without inventory check against `LEGACY.md`.

---

## 12. Testing & quality bar

- `pytest tests/test_keel_*.py` (+ quarantine tests) must stay green before merge.  
- Domain/factors/risk/policy unit-tested offline.  
- OKX adapter tests use injectable/mock HTTP (no live network required in CI).  
- **Paper acceptance (no keys):** `./scripts/run_acceptance.sh` and `tests/test_acceptance_paper.py` — temp ledger, empty OKX keys → PaperExchange, assert decisions / cycle summary.  
- Manual demo trial: `.env` with demo keys → `python -m keel.worker --once` → verify ledger + `/api/v1/*` (operator-local keys only; see RUNBOOK.md).

---

## 13. Demo trial acceptance (operator)

1. `KEEL_OKX_ENV=demo` + three demo credentials in `.env`  
2. `build_exchange()` selects OKX REST (not paper)  
3. One worker cycle completes without shell CLI  
4. Ledger contains decisions/events; API returns them  
5. Default policy may be `stub`/`rule` for connectivity; LLM optional

**Note:** Paper gate is automated (`scripts/run_acceptance.sh` / `tests/test_acceptance_paper.py`). Demo still requires **operator-local** keys in `.env` (never commit; never paste into chat). See [RUNBOOK.md](RUNBOOK.md).

---

## 14. Open decisions (resolve in addenda)

| ID | Question | Default until decided |
|----|----------|------------------------|
| O1 | Vue reuse depth (full admin vs monitor-only) | **Monitor-only** (R20 `/admin` removed; Keel admin = future addendum) |
| O2 | API auth for non-local binds | Optional `KEEL_API_TOKEN` (Bearer / X-API-Key on `/api/v1/*`) |
| O3 | When to hard-delete `r20_*` packages | **Done** — `r20_backend/` + `r20_gateway/` + `r20-*.service` removed |

---

## 15. Implementation order (post-spec)

1. Freeze this SPEC on `main` — **done**  
2. Demo trial (keys in local `.env`, not chat) — **paper gate done** (`run_acceptance.sh` + pytest); **demo pending keys**  
3. Phase U1 UI rebind to Keel API — **done**  
4. Retire `r20_backend.app` as a documented/runnable entry (soft-block) — **done**  
5. Phase U2 drop Jinja `dashboard/` — **done**  
6. Remove Vue `/admin` product surface — **done**
7. Remove `r20_backend` `/api/v1/admin/*` + `admin_auth` — **done**
8. Hard-delete `r20_gateway/` + remaining helpers — **done**  
9. O3 hard-delete `r20_backend` stubs + old systemd units — **done**  

---

## 15b. Phase E0/E1/E2A — full-gate measurement + trend-follow variant (gen-2)

**E0 freeze:** do not re-enable `KEEL_SHADOW_NEAR_PROBE`, do not lower the ~10 bps fee hurdle, do not clear kill from near-probe evidence, do not add near-probe tweaks.

**E1:** A *full-gate fire* is a rule decision with action ∈ {BUY_LONG, SELL_SHORT} and `signal_diag.missing == []`. Count distinctly from WAIT/near. `GET /api/v1/stats/quality` exposes `full_gate_fires` (`count` / `by_action` / `by_instrument`) and `economic_evidence` (`none`|`probe`|`full_gate`|`mixed`). Offline markout: `scripts/full_gate_markout.py` (or `near_entry_markout.py --full-gate-only`) reports fee-aware net RT at 60/300/900s; prefer matched non-probe `shadow_fill`, else counterfactual entry. Success criteria later: n≥20 and 5m netRT win≥0.55. Economic arming may annotate `economic_sample_source` / `full_gate_fires` without changing gate thresholds. See RUNBOOK.

**E2A (trend-follow variant):** Opt-in `KEEL_RULE_VARIANT=trend_follow` (default `mean_revert` preserves bit-for-bit existing rule). Under TF: hard-require 15m+1h same direction; RSI long = not overbought (`rsi_14 ≤ KEEL_RULE_TF_RSI_LONG_MAX`, default 68); RSI short = not oversold (`rsi_14 ≥ KEEL_RULE_TF_RSI_SHORT_MIN`, default 32); MACD/EMA/volume unchanged. Policy name stays `rule`. `signal_diag.rule_variant` + status/config `rule_variant`. Flip only in local `.env`; E0 freeze still active. Success = `full_gate_fires > 0` under TF while kill+shadow. See RUNBOOK.

## 16. Changelog

| Date | Note |
|------|------|
| 2026-09-07 | **E2A trend-follow rule variant**: `KEEL_RULE_VARIANT=mean_revert\|trend_follow`; TF forces 15m+1h + RSI not-OB/OS (68/32); policy name stays `rule`; status/config `rule_variant`; E0 freeze unchanged |
| 2026-09-07 | **E1 full-gate fires**: track BUY_LONG/SELL_SHORT with `signal_diag.missing==[]` (rule); `/stats/quality` exposes `full_gate_fires` + `economic_evidence`; `scripts/full_gate_markout.py` / `--full-gate-only`; Monitor chips; E0 freeze keeps near_probe off / 10bps hurdle / kill uncleared |
| 2026-09-07 | **Per-instrument quality/economic**: `/stats/quality` + `/stats/shadow` (+ arming/`first_live` economic) optional `by_instrument` maps for BTC/ETH/SOL diagnosis; Monitor soft-fail chips; aggregate gates unchanged; never clears kill |
| 2026-09-07 | **S2 first-live checklist**: `status.first_live` aggregates kill/shadow/capability + arming/economic + suggested `KEEL_LIVE_MAX_*` + `allowed_now` (false while kill on or economic fail) + `human_steps`; Monitor「First live」card; RUNBOOK Stage T gate; never auto-clears kill |
| 2026-09-07 | **Phase R Rule v3 edge pack**: audit volume_ratio (= last/mean20, correct); default min_vol 1.0→0.5 + percentile/soft volume paths; signal_diag edge hints (atr/expected_tp/edge_hint_bps); compare_rule_params missing-gate hist; near-probe 10bps fee hurdle unchanged |
| 2026-09-07 | **Phase R RSI soft + edge_hint wire**: hard RSI defaults 42/58→45/55; soft RSI relax when other four gates pass (48/52); near-probe prefers signal_diag.edge_hint_bps; 10bps hurdle unchanged |
| 2026-09-07 | **S1 economic arming gates**: `evaluate_arming` requires shadow markout evidence (min fills/probe, 300s sample, probe net-RT win_rate≥0.55, avg net-RT≥0); `insufficient_shadow_markout_sample` when undersampled; `arming.economic` on status + Monitor; kill still manual; RUNBOOK/SPEC |
| 2026-09-07 | **Phase R6 1h edge_hint boost**: `KEEL_RULE_1H_EDGE_BOOST` (default 1.25x, clamp 1–2, +5 bps uplift cap) when `trend_1h_confirm`+nearest aligns; Monitor/Factors multi-TF; probe 10bps hurdle unchanged |
| 2026-09-07 | **Phase R5 real multi-TF trends**: distinct `trend_15m/1h/4h` from OKX `15m/1H/4H` (or synthetic subsample); rule entry=15m; soft `KEEL_RULE_REQUIRE_1H_TREND` (default 0); signal_diag trend_gate |
| 2026-09-07 | **Phase R4 fee-aware rule suggest**: `scripts/suggest_rule_params.py` + `keel.ledger.rule_suggest` grid-search RSI/vol/rsi_relax on okx_public cohort; rank by edge≥10bps fires under fire-rate cap; recommend-only (no .env write) |
| 2026-09-07 | **Q3.4 near-probe fee edge hurdle**: gate `KEEL_SHADOW_NEAR_PROBE` on estimated `edge_bps` ≥ OKX RT/open fee (or `KEEL_SHADOW_NEAR_PROBE_MIN_EDGE_BPS`); fail-closed; audit + status hurdle fields; RUNBOOK/SPEC |
| 2026-09-07 | **Q3.3 fee-aware shadow markout**: OKX `makerU`/`takerU` (or Regular 2/5 bps fallback) nets on `/stats/shadow*`; `fee_model` + net open/RT fields; optional funding at 00/08/16 UTC; Monitor prefers netRT; RUNBOOK/SPEC cite OKX fee docs |
| 2026-09-07 | **Q3.2 shadow markout**: offline markout vs later factor_snapshots on `/stats/shadow` (+ `/stats/shadow_markout`); avg/median bps + win_rate by horizon; Monitor probe mk chip; RUNBOOK/SPEC |
| 2026-09-07 | **Q3 shadow near-probe**: `KEEL_SHADOW_NEAR_PROBE` converts strong WAIT near-signals → shadow_fill when kill+shadow on (never live); cooldown/max_missing gates; `policy=shadow_near_probe` audit; arming counts probe fills; RUNBOOK/SPEC |
| 2026-09-07 | **Q2.2 quality scorecard**: `GET /api/v1/stats/quality?hours=` compact observe health (market_source breakdown, wait/near_signal rates, shadow nest, cycles); Monitor Overview chips; RUNBOOK note |
| 2026-09-07 | **Q2.1 ledger rule-param compare**: `scripts/export_decisions.py` + `KeelLedger.export_decisions`; `compare_rule_params.py --from-ledger` / `--db` replays RuleDecisionPolicy on observed okx_public calculus/factors (skip incomplete) |
| 2026-09-07 | **Q2 decision-quality deepening**: stamp `calculus_data.market_source`; `GET /stats/decisions?market_source=`; `GET /stats/shadow`; Monitor shadow chip + market_source chip; `scripts/compare_rule_params.py` offline A/B thresholds |
| 2026-09-07 | **Q1 shadow rehearsal + live caps**: arming checks recent `shadow_fill`; kill+shadow allows shadow_fill; `KEEL_LIVE_MAX_*` for real live; Monitor/config expose |
| 2026-09-07 | **Q1 shadow fills**: `KEEL_SHADOW_MODE` → settings.shadow_mode; orchestrator ledgers `shadow_fill` (+ synthetic trade) instead of place_order; kill-switch still blocks; status/config + Monitor badge; RUNBOOK rehearse shadow before clearing kill-switch |
| 2026-09-07 | **Q1 arming checklist**: `evaluate_arming` → `status.arming` (ready_to_arm/blockers/warnings); Monitor「实盘准入」card; never auto-clears KEEL_KILL_SWITCH |
| 2026-09-07 | **Q1 key capability probe**: `okx_capability` on `/status`+`/config` (none/paper/read/trade/error) via balance + orders-pending (no place/cancel/close); ~60s cache; Monitor Credentials badge; RUNBOOK arming requires `trade` before clearing kill-switch |
| 2026-09-07 | **Q0 observe harden + near-signal alerts**: port/pid verify in observe_up; notify `alert` on near-signal; RUNBOOK alerts_only |
| 2026-09-07 | **Q0 observe stack**: `scripts/observe_{up,down,status}.sh` one-command api+worker hang; RUNBOOK 观测模式 |
| 2026-09-07 | **Q0 near-signal radar**: `GET /api/v1/signals/nearest` + Overview「近信号雷达」card (latest per-inst `signal_diag`, soft-fail) |
| 2026-09-07 | **Q0 observation productization**: rule `signal_diag` (nearest/missing gates) in calculus_data + Decisions UX; `KEEL_OBSERVE_PRESET` cadence (default/fast/slow) on `/config`; live kill-switch observation docs |
| 2026-09-07 | Monitor/API expose candle quality: Factors `data_quality_reason`, `last_cycle.market_source` (okx_public|synthetic|mixed|unknown) |
| 2026-09-07 | Worker cycle uses OKX **public candles** on live/demo REST (synthetic fallback); rule policy v2 adds EMA stack + volume_ratio filters (optional `KEEL_RULE_*` knobs) |
| 2026-09-04 | **P2 decision quality**: persist `policy_name` / `prompt_modules` on decisions (+ calculus_data); `GET /api/v1/stats/decisions?hours=`; monitor Overview stats card + Decisions policy column; `scripts/compare_policies_paper.py` offline stub↔rule compare |
| 2026-09-04 | **P1 ops desk**: actionable notify (alert/severity/text, alerts-only, discord JSON shape); monitor duty narrative (日损剩余 + config limits/instruments); deploy one-path `install.sh` + `ops_smoke.sh`. P1 ops items **done**; demo keys still pending |
| 2026-09-04 | P0 ops: RUNBOOK.md + `scripts/run_acceptance.sh` + paper pytest; SPEC §12/§13/§15 paper gate done / demo pending keys |
| 2026-09-07 | `get_settings` loads repo-root `.env` (no override of existing env); live-only ops path documented when no demo keys |
| 2026-09-04 | Max notional/contracts gate (`KEEL_MAX_NOTIONAL_PER_INSTRUMENT` / `KEEL_MAX_CONTRACTS_PER_INSTRUMENT`); expose on `/config` |
| 2026-09-04 | Optional `KEEL_API_TOKEN` bearer/X-API-Key on `/api/v1/*`; monitor risk budget + positions UPL |
| 2026-09-04 | Monitor: Factors live-candles toggle (`?live=1` + source badge); Decisions `inst_id` filter |
| 2026-09-04 | Monitor Trades `inst_id` filter (mirror Decisions); `/ready` adds worker lag / `worker_stale` |
| 2026-09-04 | Monitor Events `inst_id` + `event_type` filters (mirror Decisions/Trades) |
| 2026-09-04 | Configurable trader cycle interval (`KEEL_CYCLE_INTERVAL_SECONDS`, default 900); `/ready`+status stale = max(2×interval, interval+300); monitor shows 周期 |
| 2026-09-03 | Initial SPEC v1 drafted after stages 2–7 refactor |
| 2026-09-03 | §11: U1 done; soft-block `r20_backend.app`; supported = keel-api + keel-worker + U1 UI |
| 2026-09-03 | §10/§11: U2 done — Jinja `dashboard/` removed; `/legacy` gone; `r20_backend` admin-only remnant |
| 2026-09-03 | Removed Vue `/admin/*` product surface; admin features deferred to future Keel admin API |
| 2026-09-03 | Removed `r20_backend` `/api/v1/admin/*` + `admin_auth`; stub returns 410 |
| 2026-09-03 | Elegance: Pydantic API schemas; domain owns Decision/records; kinematics in keel.factors; calculus_engine shim |
| 2026-09-04 | Inventory-gated delete: `r20_backend/okx_client.py`, `prompt_views.py` (0 refs); QQ stack later dropped |
| 2026-09-04 | Inventory-gated delete: `scripts/calculus_engine.py` (migrated to kinematics), `r20_gateway/agents.py`, `supervisor.py` (0 refs); trader shims kept |
| 2026-09-04 | Optional `keel.notify` stub port (Null/Webhook); wire into worker cycle via `KEEL_NOTIFY_WEBHOOK_URL` |
| 2026-09-04 | `GET /api/v1/status` exposes optional `last_cycle` from ledger `worker_cycle_summary` |
| 2026-09-04 | Wire real kill switch: `KEEL_KILL_SWITCH` → settings → risk gates; expose on status/config |
| 2026-09-04 | Monitor UI: read-only kill_switch badge/banner on Overview (env-only; no toggle) |
| 2026-09-04 | `last_cycle.duration_ms` wall-clock cycle duration; delete unused `r20_backend/account_baseline` |
| 2026-09-04 | Inventory-gated delete: `r20_backend/okx_trade_service.py` (tests-only refs; Keel owns OKX REST) |
| 2026-09-04 | Monitor Last worker cycle: `risk_denies` count badge (amber when >0, muted at 0) |
| 2026-09-04 | `last_cycle.risk_deny_reasons`: capped `{gate, reason}` list alongside `risk_denies` count |
| 2026-09-04 | Inventory-gated delete: QQ stack (`qq_bind`, `qq_gateway_daemon`, `audit`) + `r20_gateway/plugins.py`; Keel uses `keel.notify` |
| 2026-09-04 | Monitor Last worker cycle: `errors` count badge (rose when >0, muted at 0) + truncated `inst_id: error` preview |
| 2026-09-04 | Typed `last_cycle.errors` as `CycleError` (`inst_id?`, `error`) matching frontend/`RiskDenyReason` style |
| 2026-09-04 | `last_cycle.error_count` full count + capped `errors` list (CYCLE_ERRORS_CAP=20); delete `scripts/qq_notifier.py` |
| 2026-09-04 | Inventory-gated delete: `r20_backend/council_manager.py` (+ `tests/test_council_manager.py`); strip council from `ai_brain_trader` (SPEC non-goal) |
| 2026-09-04 | Inventory-gated delete: `r20_backend/okx_setup.py` + `scripts/r20_okx_setup.py` (+ test); install.sh → `KEEL_OKX_*` / keel.exchange |
| 2026-09-04 | Inventory-gated delete: `scripts/ai_brain_trader.py` (council already gone; product path `python -m keel.worker`) |
| 2026-09-04 | Inventory-gated delete: `scripts/ai_factor_trader.py` (~90k OKX-CLI/shim); product path `python -m keel.worker` / `keel.worker.cycle` only |
| 2026-09-04 | Inventory-gated delete: `scripts/db_manager.py` (zero refs after ai_factor_trader drop) |
| 2026-09-04 | `GET /api/v1/pnl/daily`; status `seconds_since_last_cycle`; richer non-secret config; monitor Overview PnL/lag/config strip |
| 2026-09-04 | Default Keel scheduler trader-only; legacy script jobs opt-in via `KEEL_ENABLE_LEGACY_SCHEDULER_JOBS`; `/config` exposes `scheduler_jobs` / `legacy_scheduler_jobs` |
| 2026-09-04 | Drop legacy scheduler scripts + `KEEL_ENABLE_LEGACY_SCHEDULER_JOBS`; jobs=trader only; `/config` keeps `scheduler_jobs` |
| 2026-09-04 | Hard-delete `r20_gateway/` + helpers (`config`, backup_*, llm_manager, settings_store, notifications, schedule_store, net_security`); keep soft-block stubs; remove `r20-gateway.service` |
| 2026-09-04 | O3: hard-delete `r20_backend/` stubs + `r20-quantum`/`r20-scheduler` units + `keel/legacy`; supported = keel-api + keel-worker + optional Vite |

---

## Addendum: last_cycle on status

After each `keel.worker.cycle` run, the ledger records a `worker_cycle_summary` event (via `KeelLedger.record_cycle_summary`). `GET /api/v1/status` includes optional `last_cycle` with timestamp, mode/adapter, policy, instruments, `decision_counts`, `risk_denies` (int count, backward compatible), `risk_deny_reasons` (capped list of `{gate, reason}` objects, default cap 20), `error_count` (full non-risk error count), `errors` (capped list of `{inst_id?, error}` / `CycleError`, default cap 20), and wall-clock `duration_ms`. Monitor Last worker cycle panel shows a `risk_denies` badge (amber when >0, muted at 0) and, when present, a compact truncated preview of deny reasons under the badge; likewise an `errors` badge from `error_count` (fallback `errors.length`; rose when >0, muted at 0) with truncated `inst_id: error` lines (capped list + leftover count in title tooltip).

## Addendum: kill switch (hard gate)

`KEEL_KILL_SWITCH` (default off) loads into `settings.kill_switch`. `KillSwitchGate` and the execution orchestrator honor it: when on, all trading gate actions are denied (fail-closed); policy `WAIT` never reaches gates. Non-secret flag is echoed on `GET /api/v1/status` and `GET /api/v1/config`. No HTTP trade triggers and no admin UI toggle in v1 — env / process restart to arm. Monitor Overview shows a read-only badge/banner when `kill_switch` is true (hidden when false).

## Addendum: shadow execution

`KEEL_SHADOW_MODE` (default off) loads into `settings.shadow_mode`. When on, `ExecutionOrchestrator` still runs validation + risk gates. **Kill-switch means no real exchange orders**; with shadow on, gates allow the shadow path so operators can rehearse while `KEEL_KILL_SWITCH=1`. Decisions that would place an order instead record a ledger `shadow_fill` event (decision details) and an optional synthetic trade marked `metadata.shadow=true` / `strategy_tag=keel-shadow`, returning `success=true` without calling exchange `place_order` (no OKX POST). Non-secret `shadow_mode` is echoed on status/config; Monitor Overview shows a read-only badge/banner when on. Arming warns (or blocks when `KEEL_ARMING_REQUIRE_SHADOW=1`) if no recent `shadow_fill` (see RUNBOOK). **S1**: when `KEEL_ARMING_ECON_ENABLED` (default on), `ready_to_arm` also requires fee-aware shadow markout gates (min fills **or** probe fills, min 300s sample, `probe_win_rate_net_roundtrip` ≥ threshold, `avg_net_roundtrip_markout_bps` ≥ 0). Undersampled → blocker `insufficient_shadow_markout_sample` (not a pass). `below_hurdle`-dominated probe skips do not block alone. `status.arming.economic` exposes the summary (optional `by_instrument` snapshots for diagnosis; gate remains aggregate); kill-switch remains manual.

## Addendum: Q3 shadow near-signal probe

`KEEL_SHADOW_NEAR_PROBE` (default **off**) optionally converts strong WAIT near-signals into shadow-only fills for arming rehearsal. Requires **all** of: kill-switch on, shadow mode on, probe on. Gate: `signal_diag.nearest` ∈ {long, short} and `len(missing) ≤ KEEL_SHADOW_NEAR_PROBE_MAX_MISSING` (default 2). Per-instrument cooldown via `KEEL_SHADOW_NEAR_PROBE_COOLDOWN_SECONDS`. Policy ledger rows stay WAIT; execution synthesizes BUY_LONG/SELL_SHORT through the existing shadow_fill path with `policy=shadow_near_probe` / `probe=true` / `strategy_tag=keel-shadow-near-probe`. **Never** calls exchange `place_order`. Probe fills count toward arming shadow rehearsal. `/stats/shadow` exposes `probe_count` + `by_policy`. See RUNBOOK.

## Addendum: Q3.4 fee-aware near-probe edge hurdle

Near-probe must clear a fee-aware minimum edge before emitting `shadow_fill` (live markout after OKX fees was net-negative on small samples). Reuses `keel/exchange/okx_fees.py` (same as Q3.3): role from `KEEL_SHADOW_FEE_ROLE`, live makerU/takerU or Regular fallback (taker 5 bps / maker 2 bps per leg). Default hurdle = `round_trip_fee_bps` (taker→10, maker→4 with Regular); `KEEL_SHADOW_NEAR_PROBE_EDGE_MODE=open` uses one leg; optional `KEEL_SHADOW_NEAR_PROBE_MIN_EDGE_BPS` overrides (0 disables). Crude `edge_bps` from ATR/price × EV of probe TP/SL (2.2/1.0 ATR) with p ≈ gate completeness × confidence/100; **fail closed** when edge cannot be estimated and hurdle > 0. Audit: `edge_bps` / `hurdle_bps` / `fee_role` on probe reason, `signal_diag`, and shadow_fill / trade metadata. Status/config expose `shadow_near_probe_edge_mode`, `shadow_near_probe_min_edge_bps`, `shadow_near_probe_hurdle_bps`.



## Addendum: Phase R Rule v3+ edge pack

Rule policy v3+ keeps the five-gate stack (RSI / trend / MACD / EMA / volume) but fixes volume then RSI bottlenecks without changing market truth:

- **Semantics**: `volume_ratio = last_15m_volume / mean(last_20)` (see `compute_volume_ratio` in `keel/worker/cycle.py`). Not a percent; 1.0 = average.
- **Volume defaults**: `KEEL_RULE_MIN_VOLUME_RATIO` **0.5** (was 1.0). Optional adaptive `KEEL_RULE_MIN_VOLUME_PERCENTILE` (default 55) and soft confirmation (`KEEL_RULE_VOLUME_SOFT_*`) when the other four gates pass with a strong RSI extreme.
- **RSI defaults**: hard bands **45 / 55** (was 42 / 58). Soft RSI relax (`KEEL_RULE_RSI_RELAX_*`, default 48 / 52) when trend+macd+ema+volume already pass — occasional full fires, not every bar.
- **Diagnostics**: `signal_diag` exposes volume/RSI path + soft flags, `near_ready`, and ATR-based `atr_bps` / `expected_tp_bps` / `edge_hint_bps`. Near-probe prefers `edge_hint_bps` when finite; fee hurdle unchanged (~10 bps taker RT).
- **Safety**: Q3.4 near-probe fee hurdle unchanged; kill-switch uncleared; no live orders from this pack.
- **Verify**: `scripts/compare_rule_params.py` prints missing-gate histograms on ledger cohorts.

## Addendum: Phase R5 real multi-timeframe trends

`MarketSnapshot.trend_15m` / `trend_1h` / `trend_4h` are computed independently in `enrich_snapshot` from each TF’s closes (EMA stack + `classify_trend`). OKX public cycles fetch `15m` / `1H` / `4H` candles; paper/synthetic uses subsampled series. Rule entry gate = **15m**; `KEEL_RULE_REQUIRE_1H_TREND` (default **0**) optionally hard-requires 1h same direction. `signal_diag` documents `trend_gate`, `trend_1h_confirm`, and all three trend fields. Does not clear kill-switch or place live orders.

## Addendum: Phase R6 1h-confirm edge_hint boost

When `trend_1h_confirm` is true and `nearest` ∈ {long, short} aligns with `trend_15m`, `edge_hint_bps` is multiplied by `KEEL_RULE_1H_EDGE_BOOST` (default **1.25**, clamped **[1.0, 2.0]**). Absolute uplift is capped at **+5 bps** (`min(base*mult, base+5)`), and never exceeds `expected_tp_bps`. Audit fields: `edge_hint_1h_boosted`, `edge_hint_boost_mult`, `edge_hint_bps_raw`. Near-probe still prefers `edge_hint_bps` but the **~10 bps fee hurdle is unchanged**. Monitor Overview / Factors show multi-TF trends; `last_cycle.instrument_trends` is optional. Never clears kill-switch or places live orders.

## Addendum: Phase R4 fee-aware Rule param suggest

Offline grid search over modest Rule thresholds on an `okx_public` ledger cohort (`scripts/suggest_rule_params.py`, helpers in `keel.ledger.rule_suggest`):

- Loads decisions via existing export/replay (`--db` / `--from-ledger`, same as Q2.1).
- Grid: RSI long max {40,42,45,48}, short min {52,55,58,60}, min_vol {0.35,0.5,0.7}, optional `rsi_relax` on/off.
- Per combo: action histogram, near-signal rate, missing-gate tops, fraction with `edge_hint_bps ≥ hurdle` (default **10**), full-fire count.
- Rank: fires with edge≥hurdle, under fire-rate cap (default ≤25% cohort), fewer `volume_ok`-only misses.
- **Recommend only** — never auto-writes `.env`. Avoid blindly setting `short_min=40`.

## Addendum: Q3.2 shadow markout

Read-only outcome stats for ledger `shadow_fill` events. For each fill, look up a later price from `factor_snapshots` (preferred) or `decisions.entry_price` at horizons 60s / 300s / 900s. Directional markout in bps; aggregates include avg/median, win_rate (markout>0), probe-only subset, optional by_action. Unavailable later prices are skipped (counted). Exposed on `GET /api/v1/stats/shadow?hours=` nested `markout` and sibling `/stats/shadow_markout`. Never places orders or clears kill-switch. Monitor soft-fails if nest absent.

## Addendum: Q3.3 OKX fee-aware shadow markout

Extends Q3.2 with official OKX trading-fee awareness (not a naive fixed haircut):

- Prefer live `GET /api/v5/account/trade-fee?instType=SWAP` and use **`makerU`/`takerU`** for USDT-margined swaps (e.g. `BTC-USDT-SWAP`); crypto-margined `maker`/`taker` are wrong for these instruments. See OKX [Get fee rates](https://www.okx.com/docs-v5/en/#trading-account-rest-api-get-fee-rates) and [makerU/takerU announcement](https://www.okx.com/help/okx-will-make-changes-to-the-get-fee-rates-interface).
- Cache ~1h; soft-fail → **Regular** USDT-margined futures/swap schedule: maker **0.0200% (2 bps)**, taker **0.0500% (5 bps)** ([fee schedule](https://www.okx.com/fees), [how to calculate](https://www.okx.com/help/how-to-calculate-the-contract-transaction-fee)).
- `KEEL_SHADOW_FEE_ROLE` default **taker**; optional `KEEL_SHADOW_*_FEE_BPS` overrides (`source=override`). Round-trip = 2 × role (negative maker = rebate, sign preserved).
- Keep `avg_markout_bps` as **gross**; add `avg_net_open_markout_bps` / `avg_net_roundtrip_markout_bps` (+ median/probe/`win_rate_net_roundtrip`). Top-level `fee_model`.
- Funding is separate (position × funding rate at settlement). v1: if fill→horizon crosses standard UTC 00/08/16 and public `/api/v5/public/funding-rate` is available, apply once; else `funding_applied=false` (do not invent). Short horizons usually 0.
- Monitor chip prefers **net roundtrip** when present. Never places orders or clears kill-switch.


## Addendum: S2 first-live checklist (Stage T gate)

`GET /api/v1/status` includes `first_live`: read-only aggregation of kill_switch, shadow_mode, shadow_near_probe, capability, arming ready/blockers/economic, suggested live caps (`KEEL_LIVE_MAX_*`), `allowed_now`, and ordered `human_steps` keys. `allowed_now` is **false** while kill is on or economic gates have not passed (also requires `ready_to_arm` and shadow off). Never writes env, never clears kill-switch, never places orders. Monitor Overview shows a compact「First live」card. See RUNBOOK.

## Addendum: first-live caps

`KEEL_LIVE_MAX_NOTIONAL_PER_INSTRUMENT` (default 200) and `KEEL_LIVE_MAX_CONTRACTS_PER_INSTRUMENT` (default 5) tighten MaxNotionalGate when `okx_environment=live` and `shadow_mode` is off. Paper, demo, and shadow keep `KEEL_MAX_*`. `/config` exposes `live_max_*` and `effective_max_*`.

## Addendum: max notional / contracts per instrument

`KEEL_MAX_NOTIONAL_PER_INSTRUMENT` (default **2000** USDT; aligned above `KEEL_MAX_ASSET_MARGIN` 600 × typical ~3× leverage) and optional `KEEL_MAX_CONTRACTS_PER_INSTRUMENT` (default **50**) feed `MaxNotionalGate` in the default gate chain (after KillSwitch + DailyLoss). Gate uses `GateContext.notional` (orchestrator: `margin_usdt * leverage`) plus existing position notional; contracts checked when `size` is known. `close` still passes. Limits echoed as non-secret fields on `GET /api/v1/config`.


---

## Addendum: Phase U1 delivered

Default O1 = **monitor-only**. Vue shell reused for layout/theme; data layer rebound to Keel:

| Monitor data | Endpoint |
|--------------|----------|
| connectivity | `GET /health` |
| status | `GET /api/v1/status` |
| balance / positions | `GET /api/v1/balance`, `/api/v1/positions` |
| decisions / trades / events | `GET /api/v1/decisions`, `/trades`, `/events` (`?inst_id=` on all; `?event_type=` on events) |
| factors | `GET /api/v1/factors/{inst_id}` (`?live=1` optional; monitor toggle) |

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
- Remaining `r20_backend` soft-block stubs only (`app` / `scheduler` / `__init__`); helpers deleted with gateway.

## Addendum: Unused legacy scripts / gateway tick gate

- Deleted orphan dashboard/admin-era scripts: `sync_web_data`, `daemon_web_sync`,
  `generate_snapshots`, `debug_aggregate_orders`, `debug_audit_bills`,
  `remove_retired_personal_wechat`, `cleanup_disk`, `calculus_replay`.
- Deleted `ai_factor_trader` / `ai_brain_trader` shims; trader path is `python -m keel.worker` only.
- Deleted legacy scheduler scripts (`factor_library`, news, briefing, nightly_backup,
  backup_runtime, sync_full_ledger, self_improvement, instrument_pool, okx_runtime,
  prompt_library) and removed `KEEL_ENABLE_LEGACY_SCHEDULER_JOBS` / `legacy_scheduler_jobs`.
- Later: entire `r20_gateway/` package and remaining helpers hard-deleted (see addendum).

## Addendum: Keel scheduler trader-only

- SPEC-supported runtime is **keel-api** + **keel-worker**. `KeelScheduler._default_jobs`
  schedules only `JobSpec("trader")` (interval/timeout from `KEEL_CYCLE_INTERVAL_SECONDS`).
- Legacy R20 script jobs and `KEEL_ENABLE_LEGACY_SCHEDULER_JOBS` are **removed**.
- `GET /api/v1/config` exposes non-secret `scheduler_jobs` (always `["trader"]`).

## Addendum: Typed API + domain / factors elegance

- Introduced `keel.api.schemas` Pydantic models for health/status/positions/balance/decisions/trades/events/factors.
- Moved ledger record dataclasses and `Decision` into `keel.domain` (llm re-exports Decision).
- Ported `scripts/calculus_engine` pure math into `keel.factors.kinematics` (honest names); deprecated shim later deleted after importer migration.

## Addendum: Dead r20_backend helpers removed (inventory)

- Deleted `r20_backend/okx_client.py` and `r20_backend/prompt_views.py` after repo-wide
  reference scan showed **zero** external imports/usages (admin UI already gone; Keel
  OKX REST lives under `keel`).
- Later deleted `qq_gateway_daemon` / `audit` / `qq_bind` with the QQ stack drop (see addendum below).
- No dedicated unit tests existed solely for the deleted modules.
- Deleted `r20_backend/account_baseline.py` (+ `tests/test_account_baseline.py`) — 0 importers outside mutual self/test.
- Deleted `r20_backend/okx_trade_service.py` — 0 Keel/gateway/scripts importers; trimmed OKX V5/fast-close cases from control-plane tests only.

## Addendum: More dead scripts / gateway helpers (inventory)

- Deleted `scripts/calculus_engine.py` after migrating remaining importers
  to `keel.factors.kinematics`.
- Deleted `r20_gateway/agents.py` and `r20_gateway/supervisor.py` (zero importers).
- Trader shims and legacy scheduler scripts deleted; gateway `JOBS` / `backup_job_specs`
  are empty. Product scheduling is `python -m keel.worker` only.

## Addendum: Optional notify port (stub interface)

- Added `keel.notify`: `Notifier` Protocol, `NullNotifier`, `WebhookNotifier` (POST JSON; injectable transport for tests).
- Config: `KEEL_NOTIFY_WEBHOOK_URL` via `keel.config.settings` (empty → Null).
- P1: `cycle_notify_payload` adds `risk_denies` / capped reasons, `error_count` / capped `errors`, `duration_ms`, `alert`, `severity`, human `text`.
- P1: `KEEL_NOTIFY_ALERTS_ONLY` skips non-alert cycles; `KEEL_NOTIFY_FORMAT=keel|discord` (discord → `{"content": text}` ≤1900; not a Discord product bot).
- Worker cycle optionally notifies a compact summary after each tick; notify soft-fails and never blocks trading. Null default = no network.
- **Non-goal preserved**: no QQ / WeCom / Telegram product expansion; `r20_gateway` removed.

## Addendum: QQ stack + gateway plugins removed (inventory)

- Deleted `r20_backend/qq_bind.py`, `qq_gateway_daemon.py`, `audit.py`, and `tests/test_qq_bind.py`
  after repo-wide scan showed **zero** Keel/scripts importers (daemon only spawned by bind;
  audit only imported by the daemon). Aligns with SPEC non-goal: no QQ product.
- Deleted `r20_gateway/plugins.py` (fake plugin manifests; only external ref was
  `tests/test_open_source_control.py`). Later the whole `r20_gateway/` package was removed.
- Deleted `scripts/qq_notifier.py` and removed call sites from legacy scripts (`ai_factor_trader`, harvester, daily summary, ledger sync, nightly backup).
- Keel notify remains `keel.notify` (Null/Webhook via `KEEL_NOTIFY_WEBHOOK_URL`), not QQ.

## Addendum: Multi-agent council removed (inventory)

- Deleted `r20_backend/council_manager.py` and `tests/test_council_manager.py` after
  repo-wide scan showed external refs only in `scripts/ai_brain_trader.py` + that test
  (no Keel importers). Aligns with SPEC non-goal: no multi-agent “council” platform theater.
- Stripped council import/debate path and `council_transcript` history field from
  `ai_brain_trader` (brain script later deleted; see addendum below).
- Prefer `python -m keel.worker` / DecisionPolicy for decisions — not council.

## Addendum: Legacy OKX setup helpers removed (inventory)

- Deleted `r20_backend/okx_setup.py`, `scripts/r20_okx_setup.py`, and `tests/test_okx_setup.py`
  after repo-wide scan showed **zero** Keel importers (script only imported the module;
  tests-only otherwise). Keel OKX access is `keel.exchange` REST via `KEEL_OKX_*`.
- `deploy/install.sh` no longer installs the shell `okx` CLI or chmods `r20_okx_setup`;
  it prepares the venv + `.env` and points operators at `KEEL_OKX_*` (see `env.example`).
- **Deleted** `scripts/okx_runtime.py` (selection had been inlined into later-deleted `r20_backend.config`).

## Addendum: AI brain trader removed (inventory)

- Deleted `scripts/ai_brain_trader.py` after council path was already stripped; product
  decision path is `python -m keel.worker` / DecisionPolicy (not the legacy LLM brain).
- Quarantine asserts brain gone. `llm_manager` / `telemetry` retain non-test refs via
  `self_improvement_engine` (+ tests) — later deleted with scheduler scripts.

## Addendum: AI factor trader removed (inventory)

- Deleted `scripts/ai_factor_trader.py` (~90k). Default was already a shim into
  `keel.worker.cycle`; `KEEL_USE_LEGACY=1` historical OKX-CLI loop retired with the file.
- Product entry: `python -m keel.worker` / `python -m keel.worker.cycle` only.
- Stripped `ai_factor_trader`-dependent cases from `tests/test_quant_system_calculus.py`
  (kept pure kinematics; `factor_library` integration tests removed with script).
- Removed gateway `JOBS` trader JobSpec referencing the deleted script.
- Later deleted with scheduler scripts: `instrument_pool`, `okx_runtime`, `factor_library`.

## Addendum: db_manager removed (inventory)

- Deleted orphan `scripts/db_manager.py` (zero external refs after `ai_factor_trader` drop).
  Quarantine asserts it gone.

## Addendum: r20_gateway + remaining helpers removed (inventory)

- After legacy scheduler scripts were deleted, repo-wide scan showed **zero** Keel
  importers for `r20_gateway` and the remaining `r20_backend` helpers (only
  gateway↔helper mutual refs + obsolete tests).
- **Deleted** entire `r20_gateway/` package and `deploy/r20-gateway.service`.
- **Deleted** `r20_backend` helpers: `config.py`, `backup_secrets.py`,
  `backup_store.py`, `llm_manager.py`, `settings_store.py`, `notifications.py`,
  `schedule_store.py`, `net_security.py`.
- **Later (O3):** deleted remaining `r20_backend/` stubs + `deploy/r20-quantum.service` /
  `deploy/r20-scheduler.service` + `keel/legacy/`.
- Deleted obsolete tests: `test_gateway*`, `test_notifications`,
  `test_llm_multi_provider`, `test_control_plane_v2`, `test_open_source_control`.
- Supported path remains **keel-api** + **keel-worker** (+ optional frontend).
  Notify via `keel.notify` only. See `LEGACY.md`.
