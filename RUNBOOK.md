# Keel Trader 运维手册 / Operator Runbook

> P0/P1 产品运维：paper 干跑无需 OKX key；demo 验收仅在本机 `.env` 配置密钥。  
> **切勿把 API Key / Secret / Passphrase 粘贴到聊天、Issue、PR 或截图中。密钥只留在本机 `.env`。**
>
> **推荐安装路径**：`./deploy/install.sh` → 编辑 `.env` → `INSTALL_SYSTEMD=1 ./deploy/install.sh` （或按脚本打印的 sudo 命令）→ `sudo systemctl enable --now keel-api keel-worker` → `./scripts/run_acceptance.sh`（可选 `./scripts/ops_smoke.sh`）。

---

## 1. 支持的进程 / Supported processes

| 角色 | 命令 | 说明 |
|------|------|------|
| **keel-api** | `uvicorn keel.api.app:app --host 0.0.0.0 --port 8080` | 只读控制面（`/health`、`/ready`、`/api/v1/*`） |
| **keel-worker** | `python -m keel.worker` | **唯一**调度器；循环跑 trader |
| **一次性周期** | `python -m keel.worker --once` | 跑一轮 factors→decision→risk→execution→ledger 后退出 |
| **监控 UI（可选）** | 见 `frontend/README.md`（Vite 开发服务器） | 代理 `/api`、`/health` → API `:8080` |

推荐工作目录为仓库根；激活 venv 后设置 `PYTHONPATH=.`（或用已安装的包）。

systemd：`./deploy/install.sh` 生成改写过路径的 `deploy/*.service.local`；`INSTALL_SYSTEMD=1` 安装到 `/etc/systemd/system`（非 root 时打印 sudo 命令）。详见 `deploy/README.md`。

---

## 2. Paper 干跑（无 key） / Paper dry-run (no keys)

工厂逻辑（`keel.exchange.factory.build_exchange`）：

- `force_paper=True` → 始终 `PaperExchange`
- 否则：三个 OKX 密钥都非空 → `OkxRestAdapter`；**任一为空** → `PaperExchange`

因此 **不配置 / 清空** `KEEL_OKX_API_KEY`、`KEEL_OKX_SECRET_KEY`、`KEEL_OKX_PASSPHRASE`（及 `OKX_DEMO_*` / `OKX_*` 别名）即可走 paper。

### 步骤

```bash
cd /path/to/keel-trader
source .venv/bin/activate   # 若使用 venv
export PYTHONPATH=.

# 强制 paper：清空密钥（即使本机 .env 曾 export 过）
unset KEEL_OKX_API_KEY KEEL_OKX_SECRET_KEY KEEL_OKX_PASSPHRASE
unset OKX_DEMO_API_KEY OKX_DEMO_SECRET_KEY OKX_DEMO_PASSPHRASE
unset OKX_API_KEY OKX_SECRET_KEY OKX_PASSPHRASE
export KEEL_OKX_API_KEY= KEEL_OKX_SECRET_KEY= KEEL_OKX_PASSPHRASE=

# 可选：独立账本，避免污染生产 DB
export KEEL_LEDGER_DB=/tmp/keel-paper-$$.db

python -m keel.worker --once
```

自动化验收（推荐）：

```bash
./scripts/run_acceptance.sh
# 或: PYTHONPATH=. pytest tests/test_acceptance_paper.py -q
```

### 如何确认成功

1. **CLI**：退出码 `0`，日志含 `mode=paper` / `adapter=paper`（或同类 paper 标签）。
2. **Ledger（SQLite）**：`decisions` 表至少一行，和/或 `events` 中有 `worker_cycle_summary` / `paper_cycle_complete`。
3. **API**（另开终端起 API，指向同一 `KEEL_LEDGER_DB`）：
   - `GET /api/v1/status` — 可见最近 cycle / kill_switch / exchange 模式
   - `GET /api/v1/decisions` — 有决策记录
   - `GET /health` — `status=ok`

---

## 3. OKX demo 验收（有 key 时） / Demo trial (operator-local keys)

> 密钥**只**写在本机 `.env`（`chmod 600`），**永不**提交 git，**永不**粘贴到聊天。

### 步骤

1. `cp env.example .env && chmod 600 .env`
2. 编辑 `.env`（本机编辑器）：
   - `KEEL_OKX_ENV=demo`
   - 填入三个 demo 密钥：`KEEL_OKX_API_KEY` / `KEEL_OKX_SECRET_KEY` / `KEEL_OKX_PASSPHRASE`
3. 加载环境后跑一轮：

```bash
set -a && source .env && set +a   # 或由 systemd EnvironmentFile 注入
export PYTHONPATH=.
python -m keel.worker --once
```

4. 按 **SPEC §13** 核对清单：
   1. `KEEL_OKX_ENV=demo` + 三个 demo 凭证在 `.env`
   2. `build_exchange()` 选中 OKX REST（非 paper）
   3. 一轮 worker cycle 完成（无 shell CLI）
   4. Ledger 有 decisions/events；API 可返回
   5. 默认 policy 可为 `rule`/`stub`；LLM 可选

Paper 门禁脚本 **不能**替代 demo（demo 需要操作员本机 key）。CI / `run_acceptance.sh` 只覆盖 paper。

---

## 4. 健康检查与门禁 / Health & gates

| 端点 / 开关 | 作用 |
|-------------|------|
| `GET /health` | 进程存活；返回 `status=ok`、版本、demo/live 环境标签 |
| `GET /ready` | 账本可读且 worker **未** stale；`worker_stale` = 距上次 cycle 超过 `max(2×interval, interval+300)`（默认 interval 900s → 1800s） |
| `KEEL_KILL_SWITCH=1` | 紧急熔断：风控拒绝一切交易动作（BUY/SELL/scale/close）；`WAIT` 仍可通过 |
| `KEEL_SHADOW_MODE=1` | 影子成交：风控通过后 ledger `shadow_fill`，**不**调用 `place_order`；kill-switch 只拦真实下单，shadow 仍可记录 |
| `KEEL_MAX_NOTIONAL_PER_INSTRUMENT` | 单标的名义价值上限（默认 2000 USDT） |
| `KEEL_MAX_CONTRACTS_PER_INSTRUMENT` | 单标的合约张数上限（默认 50） |
| `KEEL_MAX_POSITIONS` / `KEEL_MAX_DAILY_LOSS` / `KEEL_MAX_ASSET_MARGIN` | 持仓数、日亏、单标保证金门禁 |

配置摘要见 `GET /api/v1/config`。

---


## 4b. 周期通知 webhooks / Cycle notify

| 变量 | 默认 | 说明 |
|------|------|------|
| `KEEL_NOTIFY_WEBHOOK_URL` | 空 | 空 → NullNotifier（**无网络**）；非空 → 每轮 POST |
| `KEEL_NOTIFY_ALERTS_ONLY` | `0` | `1` 时仅当 `alert=true` 才发送：deny / error **或** near-signal（`nearest`∈{long,short} 且 `len(missing)≤2`）/ BUY_LONG|SELL_SHORT |
| `KEEL_NOTIFY_FORMAT` | `keel` | `keel` → `{"event","payload"}`；`discord` → `{"content": text}`（≤1900 字符，无需桥接） |

Payload 含 `risk_denies` / `risk_deny_reasons`（capped）、`error_count` / `errors`（capped）、`duration_ms`、`alert`、`alert_reasons`、`near_signal` / `near_signal_count`、`severity`（`ok`|`warn`|`error`）、人类可读 `text`。 With `KEEL_NOTIFY_ALERTS_ONLY=1` you get **near-signal / deny / error** alerts only (quiet WAIT far-from-signal cycles). 非密钥字段亦在 `GET /api/v1/config`（`notify_configured` / `notify_alerts_only` / `notify_format`）。

## 5. 紧急停止 / Emergency stop

1. **立即止交易**：在 `.env` / 环境中设 `KEEL_KILL_SWITCH=1`，重启或等待下一轮 cycle 生效（门禁读取 settings）。
2. **停调度器**：
   - 前台：`Ctrl-C` / `kill` worker 进程
   - systemd：`sudo systemctl stop keel-worker`（及如需 `keel-api`）
3. 确认：`GET /api/v1/status` 中 `kill_switch=true`；worker 进程已退出；不再有新 `worker_cycle_summary`。

恢复：将 kill-switch 置 `0`，再启动 worker。

---

## 6. 密钥安全 / Secrets

- **Never paste keys into chat** — 密钥仅存本机 `.env`。
- `.env` 已在 `.gitignore`；提交前用 `git status` 确认未跟踪。
- 轮换泄露的 key；demo 与 live 密钥分离。
- 验收脚本与 pytest **故意**清空 OKX 环境变量，不依赖、不读取真实凭证。

---


## 7. Decision quality / P2 + Q2 observability

Read-only decision stats (no HTTP writes):

```bash
# After paper/demo cycles have written the ledger
curl -s "http://127.0.0.1:8080/api/v1/stats/decisions?hours=24" | python -m json.tool
# Optional market_source filter (stamped on calculus_data in cycle):
curl -s "http://127.0.0.1:8080/api/v1/stats/decisions?hours=24&market_source=synthetic" | python -m json.tool
curl -s "http://127.0.0.1:8080/api/v1/stats/shadow?hours=24" | python -m json.tool
# Q2.2 — compact observation quality scorecard (okx share + near-signal + shadow)
curl -s "http://127.0.0.1:8080/api/v1/stats/quality?hours=24" | python -m json.tool
```

Response fields (`/stats/decisions`): `decision_count`, `by_action`, `by_policy`, `wait_rate` (0–1), `risk_deny_events` (`risk_gate_blocked` count), `cycle_count` (`worker_cycle_summary`), `avg_cycle_duration_ms`, `market_source` filter echo (`any`|`okx_public`|`synthetic`).

Shadow stats (`/stats/shadow`): `count`, `by_action`, `by_policy`, `probe_count`, `last_timestamp` for `shadow_fill` events, plus **`probe_skips`/`by_skip_reason`** (Q3.5), plus nested **`markout`** (Q3.2/Q3.3 offline outcome): per-horizon (`60`/`300`/`900`s) gross `avg`/`median` `markout_bps` + `win_rate` (markout>0), fee-aware `avg_net_open_markout_bps` / `avg_net_roundtrip_markout_bps` (+ median/probe/`win_rate_net_roundtrip`), optional `by_action`. Top-level **`fee_model`** (`source` live|fallback|override, `maker_bps`/`taker_bps`, `role`, `open_fee_bps`, `round_trip_fee_bps`, `funding_note`). Later price from `factor_snapshots` (fallback `decisions.entry_price`); fills without a later price are `skipped`. Sibling: `GET /api/v1/stats/shadow_markout?hours=` (same payload). Read-only; never places orders.

Quality scorecard (`/stats/quality`): single glance for observe health — `market_source` breakdown (`okx_public` / `synthetic` / `unknown`), `decision_count`, `wait_rate`, `by_action`, `near_signal_rate` (fraction of WAIT with `signal_diag.nearest` in `{long,short}`), nested `shadow` (`count` / `by_action` / `last_timestamp`), `cycle_count`, `avg_cycle_duration_ms`, optional **`by_instrument`** map (per `inst_id`: `decision_count` / `wait_rate` / `near_signal_rate` / `by_action` / `market_source`). Read-only; does not enable trading.

Shadow stats also expose optional **`by_instrument`** (`count` / `probe_count` / `by_action` / `by_skip_reason` / compact `markout_300s` net-RT avg+win when markout is computed). Arming `economic.by_instrument` (and `first_live.economic`) mirrors fill/probe/sample/net-RT per inst for diagnosis — **overall gate stays aggregate**.

Monitor Overview soft-fetches decisions + shadow stats + quality scorecard chips (wait / near / shadow / okx share; **per-inst chips for BTC/ETH/SOL when `by_instrument` present**, soft-fail if absent). Decisions table shows `policy_name` and `calculus_data.market_source` chip when present.

Offline policy / rule-param compare (no OKX keys):

```bash
PYTHONPATH=. python scripts/compare_policies_paper.py
# prints action histogram for stub and rule; exit 0

PYTHONPATH=. python scripts/compare_rule_params.py \
  --rsi-long-max-a 45 --rsi-short-min-a 55 --min-vol-a 0.5 \
  --rsi-long-max-b 45 --rsi-short-min-b 40 --min-vol-b 0.5
# same synthetic paper snaps; prints action + near_signal_rate + missing-gate histograms; exit 0

# Q2.1 — export observed ledger decisions (local SQLite, no OKX keys)
PYTHONPATH=. python scripts/export_decisions.py \
  --db data/keel_ledger.db --hours 48 --market-source okx_public \
  --format jsonl --out /tmp/keel_decisions.jsonl

# Replay RuleDecisionPolicy A/B thresholds on that cohort (preferred over synthetic):
PYTHONPATH=. python scripts/compare_rule_params.py \
  --from-ledger /tmp/keel_decisions.jsonl \
  --rsi-long-max-a 45 --rsi-short-min-a 55 --min-vol-a 0.5 \
  --rsi-long-max-b 45 --rsi-short-min-b 40 --min-vol-b 0.5

# Or one-shot from DB (default market_source=okx_public):
PYTHONPATH=. python scripts/compare_rule_params.py \
  --db data/keel_ledger.db --hours 168 --market-source okx_public \
  --rsi-long-max-a 45 --rsi-short-min-a 55 --min-vol-a 0.5 \
  --rsi-long-max-b 45 --rsi-short-min-b 40 --min-vol-b 0.5
# Replays stored factor_snapshots / signal_diag; skips incomplete rows (skipped_incomplete=N).
# Prints missing_gates[A|B]; tuned B (short_min=40) can show occasional non-WAIT on oversold-bear cohorts.
```

### Phase R — Rule v3+ volume / RSI edge (observe knobs)

`volume_ratio` semantics (unchanged market truth): **last 15m bar volume / mean(last 20 bars)**. 1.0 = average bar; live okx_public is right-skewed (p50≈0.36–0.40, p90≈0.86), so the old default `KEEL_RULE_MIN_VOLUME_RATIO=1.0` blocked most near-signals.

After volume floor 0.5, okx_public missing shifts to **RSI side gates** (esp. `rsi_short_ok`): observed RSI sits mid-band (p50≈45–46), so hard short ≥58 rarely cleared.

Rule v3+ defaults (override via `.env`, do not commit secrets):

| Knob | Default | Role |
|------|---------|------|
| `KEEL_RULE_MIN_VOLUME_RATIO` | **0.5** | Hard relative-volume floor (was 1.0) |
| `KEEL_RULE_MIN_VOLUME_PERCENTILE` | **55** | Also pass if last-bar rank in lookback ≥ 55 (0 disables; needs live enrich) |
| `KEEL_RULE_VOLUME_SOFT_ENABLE` | **1** | Soft confirm when other 4 gates + strong RSI extreme |
| `KEEL_RULE_VOLUME_SOFT_FLOOR` | **0.35** | Soft floor (~p50) |
| `KEEL_RULE_RSI_SOFT_LONG_MAX` / `_SHORT_MIN` | **35 / 65** | Stronger RSI band for soft volume |
| `KEEL_RULE_RSI_LONG_MAX` / `_SHORT_MIN` | **45 / 55** | Hard RSI bands (was 42 / 58) |
| `KEEL_RULE_RSI_RELAX_ENABLE` | **1** | Soft RSI when trend+macd+ema+volume already pass |
| `KEEL_RULE_RSI_RELAX_LONG_MAX` / `_SHORT_MIN` | **48 / 52** | Slightly looser RSI band for soft path |

`signal_diag` includes `volume_threshold`, `volume_path` (`hard|percentile|soft|fail`), `volume_soft_pass`, `rsi_path` / `rsi_soft_pass`, `near_ready`, plus `atr_bps` / `expected_tp_bps` / `edge_hint_bps`. Near-probe prefers `edge_hint_bps` when finite, else crude ATR EV; **fee hurdle (taker RT ≈ 10 bps) unchanged.** Kill-switch / no live orders unchanged.

### Phase R5 — real multi-timeframe trends

`enrich_snapshot` now classifies **distinct** `trend_15m` / `trend_1h` / `trend_4h` from each TF’s candle closes (EMA9/21/55 stack via `classify_trend`). Previously 1h/4h were copied from the 15m classification, so multi-TF gates looked real but were not.

| Source | How 1h / 4h obtained |
|--------|----------------------|
| OKX public path | `fetch_candles` bars `1H` and `4H` (limit 64); on failure, subsample finer bars |
| Paper / synthetic | Subsample 15m → 1h (`[::4]`) and 4h (`[::16]`); still independently classified |

**Rule policy**: entry trend gate remains **15m** (`trend_bullish` / `trend_bearish`). Soft 1h confirmation is recorded on `signal_diag` (`trend_1h_confirm`, `trend_gate=15m`). Set `KEEL_RULE_REQUIRE_1H_TREND=1` to hard-require 1h same direction (`trend_gate=15m+1h`). Default **0** so fires are not silently zeroed. Diag also exposes `trend_15m` / `trend_1h` / `trend_4h` / `require_1h_trend`.

| Knob | Default | Role |
|------|---------|------|
| `KEEL_RULE_REQUIRE_1H_TREND` | **0** | `0` = soft confirm (audit); `1` = hard 15m+1h alignment |

### Phase R6 — 1h-confirm edge_hint boost (fee hurdle unchanged)

When `signal_diag.trend_1h_confirm` and nearest side aligns with 15m, multiply `edge_hint_bps` by `KEEL_RULE_1H_EDGE_BOOST` (**default 1.25x**, clamped **1.0–2.0**, absolute uplift capped **+5 bps**; never above `expected_tp_bps`). Helps fee-clearing near-probes only when multi-TF agrees — **does not lower the ~10 bps taker RT probe hurdle**. Monitor Overview / Factors strip show `trend_15m` / `trend_1h` / `trend_4h` (soft-fail); status `last_cycle.instrument_trends` optional.

| Knob | Default | Role |
|------|---------|------|
| `KEEL_RULE_1H_EDGE_BOOST` | **1.25** | Multiplier on `edge_hint_bps` when 1h confirms nearest side |

### Phase R7 — near-signal edge_hint geometry (fee hurdle unchanged)

After R6, BTC often showed `trend_1h_confirm=true` / `edge_hint_1h_boosted=true` with `edge_hint_bps=0` — the old completeness×0.40 EV went ≤0 for ≥2 missing gates and ~0.7 bps for 1 missing, so the 1h boost could not help clear the ~10 bps probe hurdle.

R7 keeps the **full-gate** EV path when `missing` is empty, and for **1–2 missing** gates with `nearest∈{long,short}` estimates a conservative residual:

```
atr_bps = (atr_14 / price) * 10_000
expected_tp_bps = atr_bps * 2.2
# near (1–2 missing):
p = {1: 0.45, 2: 0.38}[n_missing]
sized_EV = 2.2*p - 1.0*(1-p)
distance_penalty_bps = Σ gate residuals:
  RSI: pts past hard band × (atr_bps / 14)
  volume: atr_bps × 0.40 × gap_frac  (gap vs hard floor; ×0.5 if ratio ≥ soft floor)
  trend/macd/ema: atr_bps × 0.30 each
edge_hint_bps = max(0, atr_bps * sized_EV - distance_penalty_bps)
cap: min(expected_tp_bps, 1.5 × atr_bps)
# ≥3 missing or no ATR → mode=none, hint=0 (fail-closed)
```

`signal_diag` adds `edge_hint_mode` (`full`|`near`|`none`), `edge_hint_sized_ev`, `edge_hint_distance_penalty_bps`, `edge_hint_distance_components`. **R6 1h boost still applies after the base hint** (and is skipped when base is already 0). **Probe fee hurdle (taker RT ≈ 10 bps) is not lowered.**

### Phase R8 — near-edge low-ATR calibration (fee hurdle unchanged)

After R7, live BTC/ETH often still showed `edge_hint_mode=near` with **edge=0**: ATR ≈ **18–40 bps** while RSI/volume distance penalties (scaled on full `atr_bps`) wiped `atr_bps * sized_EV` (~5 bps for 2-missing). Unit tests used `atr_bps≈77`, which cleared hurdles but was not representative.

R8 keeps the R7 full-gate path and fail-closed ≥3 missing, and recalibrates **near** geometry:

```
# near (1–2 missing), nearest ∈ {long,short}:
p = {1: near_p1, 2: near_p2}[n]          # defaults 0.50 / 0.45 (env-tunable)
sized_EV = 2.2*p - 1.0*(1-p)
available_ev = atr_bps * sized_EV
pen_scale = min(atr_bps, available_ev)   # leaves room when ATR is small
distance_penalty_bps = Σ vs pen_scale:
  RSI: pen_scale × (pts / rsi_atr_scale) × rsi_coef
  volume: pen_scale × vol_frac × gap_frac  (×0.5 if ratio ≥ soft floor)
  trend/macd/ema: pen_scale × binary_frac each
edge_hint_bps = max(0, available_ev - distance_penalty_bps)
```

When residuals are **modest** (e.g. volume just below hard floor, RSI within ~3 pts), hint can reach **≥10 bps** at `atr_bps≈25`. When far (e.g. RSI 15 pts past band), hint stays **<10**. **Probe fee hurdle (taker RT ≈ 10 bps) is not lowered.**

`signal_diag` adds `edge_hint_penalty_scale_bps` (= `pen_scale`) alongside existing distance components.

| Knob | Default | Role |
|------|---------|------|
| `KEEL_RULE_EDGE_NEAR_P1` | **0.50** | Near win-prob when exactly 1 gate missing (clamped 0.20–0.65) |
| `KEEL_RULE_EDGE_NEAR_P2` | **0.45** | Near win-prob when exactly 2 gates missing |
| `KEEL_RULE_EDGE_RSI_ATR_SCALE` | **14** | RSI pts divisor vs `pen_scale` |
| `KEEL_RULE_EDGE_RSI_PENALTY_COEF` | **1.0** | Extra RSI penalty multiplier |
| `KEEL_RULE_EDGE_VOL_PENALTY_FRAC` | **0.25** | Volume gap weight vs `pen_scale` (R7 was 0.40) |
| `KEEL_RULE_EDGE_BINARY_PENALTY_FRAC` | **0.25** | Binary gate weight vs `pen_scale` (R7 was 0.30) |

**Before/after @ atr_bps=25 (1 missing volume, ratio just below hard, soft-halved gap):** R7 raw ≈ 10.0 (fragile; ~0 at atr≈18 after RSI/vol wipe); R8 raw ≈ **14.6** with components audit. **RSI 15 pts away @ atr=25:** R7/R8 both → hint **0** (not spammy).

### Phase R9 — near-entry markout (counterfactual; recommend-only)

After R8, many live WAIT rows still show `edge=0` with 1–2 missing gates. Forcing `edge≥10` via more formula tweaks would game the probe hurdle. R9 **measures** whether entering on historical near-signals would have cleared fees after the fact — offline counterfactual, no live behavior change.

For each WAIT decision with `signal_diag.nearest ∈ {long,short}` and **1…`--max-missing`** gates missing (default **1–2**):

1. Resolve entry price from matched `factor_snapshots` (same cycle timestamp; ±30s fallback; else `decisions.entry_price`).
2. Map nearest → shadow action (`long→BUY_LONG`, `short→SELL_SHORT`).
3. Reuse Q3.2/Q3.3 `lookup_later_price` / `markout_bps` + OKX fee model (`net_RT = gross − round_trip_fee_bps`, optional funding).
4. Report count, by_instrument, gross/net-RT stats, `fee_model`, and **fraction clearing 10 bps net-RT at 300s**.

**Recommend-only** — do **not** flip `KEEL_SHADOW_NEAR_PROBE` or lower the fee hurdle from this script. Use the summary as evidence for whether probes/near entries are worth a manual enable.

```bash
# Offline counterfactual (local SQLite; strips OKX keys; never writes .env)
PYTHONPATH=. python scripts/near_entry_markout.py \
  --db data/keel_ledger.db --hours 168 --market-source okx_public \
  --max-missing 2 --horizons 60,300,900

# Per-instrument
PYTHONPATH=. python scripts/near_entry_markout.py \
  --db data/keel_ledger.db --hours 168 --market-source okx_public \
  --inst-id BTC-USDT-SWAP,ETH-USDT-SWAP,SOL-USDT-SWAP --json-only
```

| Knob (CLI) | Default | Role |
|------------|---------|------|
| `--max-missing` | **2** | Upper bound on `len(signal_diag.missing)` |
| `--min-missing` | **1** | Lower bound (near = at least one gate short) |
| `--horizons` | `60,300,900` | Markout horizons (seconds) |
| `--clear-hurdle-bps` | **10** | Net-RT clear fraction threshold |
| `--market-source` | `any` | `okx_public` / `synthetic` / `any` |
| `--no-funding` | off | Skip funding adj (trading fees still applied) |

Helper: `keel.ledger.near_entry_markout.compute_near_entry_markout` (same fee_model shape as shadow markout).

### Phase E0 — freeze (gen-2)

After R9 measurement, **do not** chase near-signal probe enablement:

- Keep `KEEL_SHADOW_NEAR_PROBE=0` (recommend stay **off**).
- Do **not** lower the ~10 bps fee hurdle.
- Do **not** clear kill-switch / go live from near-probe evidence.
- Do **not** add near-signal probe tweaks that game the hurdle.

E0 is a product lock: near cohort remains observational only.

### Phase E1 — full-gate fire tracking + markout

Track only **full-gate** rule fires — decisions where action ∈ `{BUY_LONG, SELL_SHORT}`, `signal_diag.missing == []` (all entry gates passed), and policy is `rule`. Distinct from WAIT / near (1–2 missing) and from forced paper fills (no `signal_diag`).

**Success later (not this PR):** n ≥ 20 full-gate samples and 5m (300s) net-RT win rate ≥ 0.55. This phase builds the measurement.

| Surface | What |
|---------|------|
| Detection | `keel.ledger.full_gate.is_full_gate_fire` / `aggregate_full_gate_fires` |
| API | `GET /api/v1/stats/quality?hours=` → `full_gate_fires.{count,by_action,by_instrument}`, `economic_evidence` (`none`/`probe`/`full_gate`/`mixed`), per-inst `full_gate_fires` |
| Markout | `scripts/full_gate_markout.py` or `scripts/near_entry_markout.py --full-gate-only` — shadow_fill entry when present, else counterfactual from decision ts; fee-aware 60/300/900 |
| Monitor | Quality bar chips `full-gate N` + `econ <evidence>`; arming economic may show `economic_sample_source` / `full_gate_fires` (flag only — gates unchanged) |

```bash
# Full-gate count + evidence (API)
curl -s "http://127.0.0.1:8080/api/v1/stats/quality?hours=24" \
  | python -c "import sys,json; d=json.load(sys.stdin); print(d.get('full_gate_fires')); print(d.get('economic_evidence'))"

# Offline full-gate markout (local SQLite; strips OKX keys; never writes .env)
PYTHONPATH=. python scripts/full_gate_markout.py \
  --db data/keel_ledger.db --hours 168 --market-source okx_public \
  --horizons 60,300,900

# Same via near-entry script flag
PYTHONPATH=. python scripts/near_entry_markout.py --full-gate-only \
  --db data/keel_ledger.db --hours 168 --market-source okx_public
```

**Recommend-only** — do **not** flip `KEEL_SHADOW_NEAR_PROBE` from this evidence yet. Prefer weighing full-gate markout for economic arming once n is large enough; until then keep near_probe off.

### Phase E2A — trend-follow rule variant (gen-2)

Live observe showed `full_gate_fires=0` under default **mean-reversion** RSI (WAIT≈99%; missing dominated by `volume_ok` + `rsi_long/short_ok`). E2A adds an **opt-in** rule variant that can full-gate fire in trends **without** replacing the default.

| Knob | Default | Notes |
|------|---------|--------|
| `KEEL_RULE_VARIANT` | `mean_revert` | `trend_follow` enables E2A semantics |
| `KEEL_RULE_TF_RSI_LONG_MAX` | `68` | Long OK when RSI **not overbought** (`rsi_14 ≤ max`) |
| `KEEL_RULE_TF_RSI_SHORT_MIN` | `32` | Short OK when RSI **not oversold** (`rsi_14 ≥ min`) |

When `trend_follow`:
- **Hard-require** 15m+1h same direction (forces `require_1h` behavior regardless of `KEEL_RULE_REQUIRE_1H_TREND`). E3.1 adds optional/default 4h via `KEEL_RULE_TF_REQUIRE_4H`.
- Keep MACD / EMA / volume gates (volume soft path still OK).
- RSI side gate **keys** stay `rsi_long_ok` / `rsi_short_ok` so E1 full_gate + near-signal UX keep working.
- `signal_diag.rule_variant` + reason strings mention `trend_follow` when firing.
- Factory still returns `RuleDecisionPolicy` with `name=="rule"` (E1 `policy_name` detection unchanged).
- Status/config echo `rule_variant` next to `decision_policy`.

**How to flip (local `.env` only — never commit):**
```bash
# In local .env (kill+shadow still on; E0 freeze still active)
KEEL_RULE_VARIANT=trend_follow
# optional:
# KEEL_RULE_TF_RSI_LONG_MAX=68
# KEEL_RULE_TF_RSI_SHORT_MIN=32
```
Restart worker after edit. Success = observe `full_gate_fires > 0` under TF while **still** kill+shadow. **Do not** enable `KEEL_SHADOW_NEAR_PROBE`, lower the 10bps hurdle, or clear kill-switch (E0 freeze).

Revert: set `KEEL_RULE_VARIANT=mean_revert` (or remove) and restart.

### Phase E2B — TF volume soft + MACD lag (gen-2)

Live TF observe (~hours) still had `full_gate_fires=0`. Missing under TF was dominated by `volume_ok`; closest BTC shorts were 15m+1h bearish with only `macd_short_ok` missing (histogram slightly positive — lag) or macd+volume. E2A residual: volume soft still required MR RSI extreme, so TF rarely got soft volume.

E2B (``trend_follow`` only; ``mean_revert`` unchanged):

| Knob | Default | Notes |
|------|---------|--------|
| `KEEL_RULE_TF_MACD_LAG_BPS` | `3.0` | Clamp 0–15. TF: long OK if `hist≥0` OR `(hist/price)*1e4 ≥ -lag`; short symmetric. `lag=0` restores strict sign. |
| (existing) `KEEL_RULE_VOLUME_SOFT_*` | — | Under TF, soft path = **trend+macd+ema** + `ratio≥soft_floor` → `volume_path=soft_tf` (**no** RSI extreme). Hard / percentile unchanged. MR soft still needs RSI extreme (`path=soft`). |

Audit: `signal_diag.macd_lag_bps`, `macd_lag_ok` (true when lag tolerance cleared a wrong-sign hist), `volume_path` (`soft_tf` when TF soft fires). Policy name stays `rule`; `rule_variant` still echoed.

**How to tune (local `.env` only — never commit):**
```bash
KEEL_RULE_VARIANT=trend_follow
# optional E2B:
# KEEL_RULE_TF_MACD_LAG_BPS=3.0
# KEEL_RULE_VOLUME_SOFT_ENABLE=1
# KEEL_RULE_VOLUME_SOFT_FLOOR=0.35
```
Restart worker after edit. **Do not** enable `KEEL_SHADOW_NEAR_PROBE`, lower the 10bps hurdle, or clear kill (E0 freeze).

**E2C offline fire-rate replay (no restart):** live observe can still show `full_gate_fires=0` when 15m/1h rarely align; soft_tf still needs trend+macd+ema first. Replay recent ledger snapshots under forced TF (E2B defaults) without touching `.env`:

```bash
# Offline TF+E2B full-gate fire-rate (local SQLite; strips OKX keys; never writes .env)
PYTHONPATH=. python scripts/tf_full_gate_replay.py \
  --db data/keel_ledger.db --hours 168 --variant trend_follow --compare-mr
```

Reports n snapshots, full_gate count/rate by instrument+action, top missing gates under TF, and 1-missing near fires. Fee-aware markout of counterfactual replay hits is skipped — use E1 `scripts/full_gate_markout.py` for live full-gate rows. Helpers: `keel.ledger.tf_fire_replay`.

### Phase E3 — rule fire cooldown + quality full-gate markout (gen-2)

Live TF observe (as of **2026-09-08**) showed spray/overtrade: `full_gate_fires≈54` in 24h (all `SELL_SHORT`; BTC 37 / SOL 17) while 15m+1h stayed bearish — fires repeating every ~cycle (~300s). Fee-aware full-gate markout 300s: `win_rate_net_roundtrip≈0.24` (need ≥0.55), `avg_net_rt≈-9.7` bps, `frac_clear_10bps≈0.15`. **E1 success criteria still fail** (~24% net win). E0 freeze still: no `near_probe` on, no hurdle cut, no kill clear, no `.env` commit.

**A) Per-instrument rule fire cooldown (TF + MR)**

| Knob | Default | Role |
|------|---------|------|
| `KEEL_RULE_FIRE_COOLDOWN_SECONDS` | **900** | After a full-gate `BUY_LONG`/`SELL_SHORT` is recorded, suppress another same-instrument full-gate entry. Clamp **0–7200**; **0** disables. |

During cooldown the worker returns **WAIT** with reason mentioning cooldown; `signal_diag` stays attached (`fire_cooldown_active`, `fire_cooldown_seconds`) so near UX still works, but the row must **not** count as `full_gate_fire` and must **not** produce a shadow fill for the suppressed entry. Implementation: `keel.execution.fire_cooldown` + worker cycle (ledger recent decisions; same pattern as near_probe cooldown).

**B) Full-gate markout on quality API + Monitor**

`GET /api/v1/stats/quality?hours=` now includes `full_gate_markout` (fee-aware, **no network**): horizons 60/300/900 with `sample_count`, `win_rate_net_roundtrip`, `avg_net_roundtrip_markout_bps`, `frac_clear_net_rt_hurdle` (10 bps). Primary convenience fields mirror the **300s** row. Monitor Quality bar shows lightweight **FG 5m net** chip.

**Arming economic (read-only):** when `full_gate_fires ≥ 20`, prefer full-gate 5m netRT metrics in the economic block / blockers (`full_gate_win_rate_net_roundtrip_*`); still never auto-clears kill. Keep near_probe off (E0).

```bash
# Quality + FG markout (API)
curl -s "http://127.0.0.1:8080/api/v1/stats/quality?hours=24" \
  | python -c "import sys,json; d=json.load(sys.stdin); print(d.get('full_gate_fires')); print(d.get('full_gate_markout'))"
```

### Phase E3.1 — TF require 4h trend alignment (quality filter)

After E3 cooldown, FG markout still fails (~24% 5m netRT win). Many historical `SELL_SHORT` full-gates had `trend_4h=neutral` while 15m+1h were bearish. Recent ETH `BUY_LONG` already had t15/t1h/t4h all bullish — that is the cohort to keep.

| Knob | Default | Notes |
|------|---------|--------|
| `KEEL_RULE_TF_REQUIRE_4H` | **1** | `trend_follow` only. When on: long needs `trend_15m`+`trend_1h`+`trend_4h` all bullish; short all bearish. Folded into `trend_bullish`/`trend_bearish` (same missing keys). `trend_gate=15m+1h+4h`. Set **0** to restore E2A 15m+1h-only. **mean_revert ignores** this env. |

Audit: `signal_diag.require_4h_trend`, `trend_4h_confirm`, `trend_gate`. Status/config echo `tf_require_4h` (effective; False under MR). Cooldown defaults unchanged. **Still E0 freeze** (no near_probe, no hurdle cut, no kill clear).

```bash
# Local .env only — never commit
KEEL_RULE_VARIANT=trend_follow
# KEEL_RULE_TF_REQUIRE_4H=1   # default; set 0 to disable 4h hard gate
```

Restart worker after edit. No live orders.

### Phase F1 — isolate pre-E3.1 full-gate cohort (measurement)

Jo observe: live economic/arming still used the **full** FG cohort dominated by pre-E3.1 spray (54/55 shorts with `trend_4h=neutral` → ~24% 5m netRT) and blocked forever. Post-E3.1 decisions have `require_4h_trend=true` and `trend_gate=15m+1h+4h` — isolate them.

| Piece | Behavior |
|-------|----------|
| Classify | `keel.ledger.full_gate.classify_full_gate_cohort` — `post_e31`/`strict_tf` when `require_4h_trend` truthy **or** `trend_gate` contains `4h`; else `pre_e31`/`stale_pre_e31` |
| Quality | `full_gate_fires.by_cohort`; primary `full_gate_markout` = **post_e31**; audit `full_gate_markout_pre_e31`; `economic_evidence` may be `stale_pre_e31` when only pre exists |
| Arming | FG win-rate gates use **post_e31 markout only**. Prefer path still when total `full_gate_fires ≥ 20` (`FULL_GATE_ECON_PREFER_MIN`). Need post_e31 markout sample ≥ `KEEL_ARMING_ECON_MIN_MARKOUT_SAMPLE` / `arming_econ_min_markout_sample` (default **5**); else blocker `insufficient_post_e31_full_gate_sample` — **never** fail on pre_e31 24%. Annotate `full_gate_fires_post_e31` / `_pre_e31` / `full_gate_cohort_used` |
| Monitor | FG 5m net chip = post primary; optional `stale pre_e31 N` hint |

**Still E0 freeze** (no near_probe, no hurdle cut, no kill clear). No push/merge/restart required for this measurement PR.

```bash
curl -s "http://127.0.0.1:8080/api/v1/stats/quality?hours=24" \
  | python -c "import sys,json; d=json.load(sys.stdin); print(d.get('full_gate_fires')); print(d.get('full_gate_markout')); print(d.get('full_gate_markout_pre_e31')); print(d.get('economic_evidence'))"
```

### Phase F2a — TF extension ATR entry filter (quality)

Jo: F0b offline under TF+4h+cooldown → ~60 FG / ~7d BTC+ETH+SOL but **5m netRT win ≈10%**, avg ≈−11 bps, frac_clear_10bps 0% — still ≪0.55. F2a is entry quality: do not chase an already-extended move.

| Env | Default | Behavior |
|-----|---------|----------|
| `KEEL_RULE_TF_MAX_EXTENSION_ATR` | **0** (off) | `trend_follow` only. Long rejects when `(price-ema_21)/atr_14 > max`; short when `(ema_21-price)/atr_14 > max`. Clamp **0.5–5** when enabled; **0 disables**. `atr_14<=0` → fail-closed for this gate when enabled. **mean_revert ignores**. |

Audit: `signal_diag.extension_atr`, `max_extension_atr`, `extension_ok`, `extension_headroom_atr` (soft near distance). Folded into `missing` / full-gate (diagnose + `rule_based_decision`). Backtest picks it up automatically via diagnose.

```bash
# Optional local .env (never commit). 0 disables for A/B.
# KEEL_RULE_TF_MAX_EXTENSION_ATR=1.5
```

Still **E0 freeze** (no near_probe, no hurdle cut, no kill clear). See F2b RSI pullback below; F2c strategy compare follows.

### Phase F2b — TF RSI pullback gate (quality)

Jo: F2a extension@1.5 hurt 5m net (10%→6.1%); product default extension stays **0=off**. F2b RSI pullback@52/48 **over-filtered** offline (FG ~60→2, 5m win 0%) — product default `KEEL_RULE_TF_PULLBACK` stays **0=off**. Set `1` to A/B enable.

| Env | Default | Behavior |
|-----|---------|----------|
| `KEEL_RULE_TF_PULLBACK` | **0** (off) | Master switch for TF pullback. `0` disables both side filters. `trend_follow` only; **mean_revert ignores**. |
| `KEEL_RULE_TF_RSI_PULLBACK_LONG_MAX` | **52** | Long: `rsi_14 <= max` → `pullback_ok`. Clamp 20–80. |
| `KEEL_RULE_TF_RSI_PULLBACK_SHORT_MIN` | **48** | Short: `rsi_14 >= min` → `pullback_ok`. Clamp 20–80. |

Audit: `signal_diag.pullback_ok`, `rsi_pullback_long_max`, `rsi_pullback_short_min`, `tf_pullback_enabled`. Folded into `missing` / full-gate (diagnose + `rule_based_decision`). Backtest picks it up via diagnose (ext still 0 unless set).

```bash
# Optional local .env (never commit). Master off for A/B vs F0b baseline:
# KEEL_RULE_TF_PULLBACK=0
# KEEL_RULE_TF_RSI_PULLBACK_LONG_MAX=52
# KEEL_RULE_TF_RSI_PULLBACK_SHORT_MIN=48

PYTHONPATH=. python scripts/okx_history_rule_backtest.py \
  --inst-ids BTC-USDT-SWAP,ETH-USDT-SWAP,SOL-USDT-SWAP \
  --bars-15m 700 --cooldown-seconds 900 --variant trend_follow \
  --json-out /tmp/keel_f2b_okx_history.json
```

Still **E0 freeze** (no near_probe, no hurdle cut, no kill clear).

### Phase F4 — multi-TF short-vs-short / long-vs-long train/valid

Jo's point (mandatory): **do not mix horizons**. Short entry TF uses short hold/markout; long entry TF uses long hold/markout. Entry bars (OKX): **5m, 15m, 30m, 1H, 4H**.

**Protocol per entry timeframe T**

1. Fetch OKX public candles for T + confirm mid/high (practical): 5m→15m/1H; 15m→1H/4H; 30m→1H/4H; 1H→4H/1D; 4H→4H/1D.
2. **Same calendar split** for all T: train **[-14d, -7d)**, valid **[-7d, now)**.
3. **Markout / barrier horizons scale with T** (bar multiples — not fixed 300s wall-clock when that mismatches T):
   - 5m → primary 3–6 bars (15–30m), selection primary **4 bars / 1200s**, secondary ~12 bars
   - 15m → primary ~4–8 bars (1–2h), selection **6 bars / 5400s**
   - 30m → primary ~4–8 bars, selection **6 bars / 10800s**
   - 1H → primary ~4–8 bars (4–8h), selection **6 bars / 21600s**
   - 4H → primary ~3–6 bars (12–24h), selection **4 bars / 57600s**
   Fee-aware net RT still applied (taker RT ~10bps; funding ignored).
4. Modest grid **per T on train only**; freeze ONE config; validate once on holdout.
5. CLI prints a table per T: train best + valid win/avg/n.

| Piece | Location |
|-------|----------|
| Horizon map + per-T grid | `keel.backtest.multitf` |
| Walk accepts `entry_bar` + bar-scaled horizons | `walk_forward_backtest` |
| CLI | `scripts/okx_multitf_train_valid.py` |
| Unit tests | `tests/test_okx_multitf_horizons.py` |

**Offline result (BTC, all five TFs, modest grid=15/T, skip-barrier):** no cell met train avg_net≥−5bps on any T. Valid primary wins: **5m 23.4%** (n=77, avg −16.4bps) / **15m 15.8%** (n=19) / **30m 38.5%** (n=13) / **1H 50%** (n=2, tiny) / **4H 0%** (n=3, tiny). All ≪0.55 (or n too small) — do not claim success / do not arm.

```bash
# Public API only — strips OKX keys / KEEL_SKIP_DOTENV (never writes .env)
PYTHONPATH=. python scripts/okx_multitf_train_valid.py \
  --inst-ids BTC-USDT-SWAP \
  --entry-bars 5m,15m,30m,1H,4H \
  --json-out /tmp/keel_f4_multitf.json
```

**Residuals:** horizon alignment is the point of F4 (short-vs-short / long-vs-long); closed-bar higher-TF + train truncate; per-T grid selection bias — valid is the only honest score. Still **E0 freeze**.

### Phase F5 — TradingView-inspired rule variants (opt-in)

Jo: `trend_follow` fails ~10bps taker RT hurdle on train/valid and live post_e31. F5 maps **public** strategy *concepts* (not copyrighted Pine) into Keel:

| Variant | Entry idea | Primary offline score |
|---------|------------|----------------------|
| `supertrend` | ATR-band **direction flip** (+ HTF filter) | `barrier_exit_markout` netRT |
| `donchian` | Close beyond **prior-N** high/low + EMA filter + volume ≥ SMA×k (no repaint) | same |
| `trend_follow` | Baseline (unchanged) | same compare |

| Piece | Location |
|-------|----------|
| Indicators | `keel.factors.technical` (`calculate_supertrend`, `donchian_prior_channel`, `volume_sma_ratio`) |
| Gates / reason codes | `keel.policy.tv_rules` + `_rule_variant()` |
| Compare CLI | `scripts/okx_tv_strategy_compare.py` |
| Tests | `tests/test_f5_tv_indicators.py`, `tests/test_f5_tv_variants.py` |

```bash
PYTHONPATH=. python scripts/okx_tv_strategy_compare.py \
  --inst-ids BTC-USDT-SWAP \
  --entry-bars 15m,30m,1H \
  --json-out /tmp/keel_f5_tv_compare.json
```

**Offline result (BTC, 15m/30m/1H, barrier primary, modest grids):** no variant cleared valid barrier win≥0.55 with avg_net≥0 and n≥20. Best-looking train cells overfit (e.g. ST 15m train bar_win 80% n=5 → valid 0% n=1). Valid barrier wins: TF 10–15% / ST tiny-n 0–50% / DC 0–20%. Honest fail — do not claim / do not flip `.env`.

| TF | variant | chosen | tr_FG | tr_bW | va_FG | va_bW | va_n | pass |
|----|---------|--------|------:|------:|------:|------:|-----:|------|
| 15m | trend_follow | 4h=1 cd=900 ext=0 | 36 | 47% | 21 | 11% | 19 | no |
| 15m | supertrend | 4h=1 st=10×2 | 5 | 80% | 1 | 0% | 1 | no |
| 15m | donchian | 4h=0 dc=20 vol≥1 | 12 | 42% | 15 | 20% | 15 | no |
| 30m | trend_follow | 4h=1 ext=1.5 | 26 | 31% | 22 | 15% | 20 | no |
| 30m | supertrend | 4h=0 st=10×2 | 4 | 50% | 2 | 50% | 2 | no |
| 30m | donchian | 4h=1 dc=20 vol≥0.8 | 4 | 25% | 2 | 0% | 2 | no |
| 1H | trend_follow | 4h=0 ext=1.5 | 26 | 31% | 14 | 15% | 13 | no |
| 1H | supertrend | 4h=0 st=14×3 | 2 | 50% | 2 | 50% | 2 | no |
| 1H | donchian | 4h=0 dc=20 vol≥0.8 | 3 | 0% | 3 | 0% | 3 | no |

**Env (opt-in only):** `KEEL_RULE_VARIANT=supertrend|donchian` plus `KEEL_RULE_ST_*` / `KEEL_RULE_DONCHIAN_*`. Code default for **unset** remains `mean_revert`; live observe keeps `.env` (`trend_follow`). Cool-down still applies. **Do not** clear kill / enable near_probe / flip live variant unless valid barrier win ≥0.55 **and** avg_net≥0 **and** n≥20. Still **E0 freeze**.



### Phase F6 — ATR trail exit + ADX regime + SuperTrend soft entry

Jo: F5 entry families failed valid (tiny-n ST, TF/DC ≪0.55). F6 focuses on **what TV quality systems do after entry** (and light regime filtering) — not more entry families.

| Piece | Behavior | Live default |
|-------|----------|--------------|
| ATR trail exit | Offline `trail_exit_markout`: stop = peak ± k×ATR (initial SL floor); optional `time_stop_bars` | backtest/compare only |
| ADX regime | Skip entries when ADX < `KEEL_RULE_ADX_MIN` (range); TF + ST + DC | **0 = off** |
| ST soft entry | `KEEL_RULE_ST_ENTRY_MODE=soft` allows side-hold entries (cooldown prevents spray); `flip` kept | **flip** |

| Piece | Location |
|-------|----------|
| ADX | `keel.factors.technical.calculate_adx` |
| Trail exit | `keel.backtest.okx_history_rule.trail_exit_markout` (+ walk `include_trail`) |
| Soft ST / ADX gates | `keel.policy.tv_rules` (+ TF fold in `stub`) |
| Compare CLI | `scripts/okx_f6_exit_regime_compare.py` |
| Tests | `tests/test_f6_adx_trail_soft.py` |

```bash
PYTHONPATH=. python scripts/okx_f6_exit_regime_compare.py \
  --inst-ids BTC-USDT-SWAP \
  --entry-bars 15m,30m,1H \
  --json-out /tmp/keel_f6_exit_regime.json
```

**Offline result (BTC, 15m/30m/1H, trail primary, modest grids):** no variant cleared valid trail|barrier win≥0.55 with avg_net≥0 and n≥20. Soft ST helped **sample size** on 1H (train FG=16 / valid n=17 vs F5 flip tiny-n) but valid win 17.65% avg −37bps — still no edge. Best valid trail wins: TF 5–7% / ST 0–50% tiny-n / DC 0%. Honest fail — do not claim / do not flip `.env`.

| TF | variant | chosen | tr_FG | tr_pW | va_FG | va_pW | va_n | pass |
|----|---------|--------|------:|------:|------:|------:|-----:|------|
| 15m | trend_follow | adx=off trail=2 tsb=12 | 36 | 39% | 21 | 5% | 19 | no |
| 15m | supertrend | flip st=10×2 trail=2 | 5 | 80% | 1 | 0% | 1 | no |
| 15m | donchian | adx≥25 trail=1.5 | 4 | 50% | 2 | 0% | 2 | no |
| 30m | trend_follow | adx≥20 trail=2 tsb=12 | 26 | 31% | 14 | 7% | 14 | no |
| 30m | supertrend | flip st=10×2 | 4 | 50% | 2 | 50% | 2 | no |
| 30m | donchian | adx≥25 | 1 | 100% | 1 | 0% | 1 | no |
| 1H | trend_follow | adx≥25 trail=1.5 | 5 | 60% | 1 | 0% | 1 | no |
| 1H | supertrend | **soft** adx≥20 st=10×2 | 16 | 44% | 17 | 18% | 17 | no |
| 1H | donchian | adx=off | 3 | 33% | 3 | 0% | 3 | no |

**Env (opt-in only):** `KEEL_RULE_ADX_MIN` (default 0), `KEEL_RULE_ST_ENTRY_MODE=flip|soft` (default flip). Trail exit is offline/compare only. Live observe keeps `.env` (`trend_follow`). **Do not** clear kill / enable near_probe / flip live variant unless valid trail|barrier win ≥0.55 **and** avg_net≥0 **and** n≥20. Still **E0 freeze**.

### Phase F3 — train / validation strategy pipeline (no peeking)

Jo protocol (offline, public OKX candles only):

1. **Analysis (train):** closed-bar decisions in **[-14d, -7d)** — fit / grid rank here only.
2. **Validation (valid):** closed-bar decisions in **[-7d, now)** — score the frozen config once.
3. Mathematical search (variant / require_4h / extension ATR / pullback RSI bands / cooldown) runs **only on train**; markout path for train truncates candles at `train_end` so valid prices are not visible.
4. **Select ONE** primary by pre-declared rule: maximize train 5m netRT win subject to train FG **n≥10** and train avg netRT **≥ −5bps** (else best available — say so).
5. Validate once vs **F0b baseline** (TF + require_4h + ext=0 + pullback=0 + cd=900) on the **same valid window**. Do **not** claim success if valid 5m win ≪ **0.55**.

**Offline result (BTC+ETH+SOL, 2200×15m, grid=42):** no cell met train avg_net≥−5bps; best-available train pick `TF|4h|ext=1.5|pb=off|cd=1800` train FG=36 / 5m win **22.2%** avg **−7.9bps**. Frozen valid: FG=62 / 5m win **8.1%** avg **−10.7bps** frac_clear **0%** (900s win 19.4%; barrier win 19.4% timeout56/SL6). F0b baseline on same valid: FG=92 / 5m win **12.0%** avg **−10.7bps**. Valid ≪0.55 — do not claim success / do not arm.

```bash
# Public API only — strips OKX keys / KEEL_SKIP_DOTENV (never writes .env)
PYTHONPATH=. python scripts/okx_train_valid_strategy.py \
  --inst-ids BTC-USDT-SWAP,ETH-USDT-SWAP,SOL-USDT-SWAP \
  --bars-15m 2200 \
  --json-out /tmp/keel_f3_train_valid.json
```

| Piece | Location |
|-------|----------|
| Windows + grid + selection | `keel.backtest.train_valid` |
| Walk + decision window | `walk_forward_backtest(..., decision_ts_min/max=...)` |
| CLI | `scripts/okx_train_valid_strategy.py` |
| Unit tests | `tests/test_okx_train_valid_strategy.py` |

**Residuals:** closed-bar higher-TF filter + train truncate (look-ahead control); taker RT fee model / funding ignored; modest grid selection bias on train win rate — valid is the only honest score.

Still **E0 freeze** (no near_probe, no hurdle cut, no kill clear, no arm from F3 alone).

### Phase F2c — strategy compare on same candles (measurement)

Jo: F2a/F2b entry filters did not lift 5m net toward **0.55**. F2c asks whether **mean_revert** beats TF E3.1 on the same public candles, and whether a **barrier exit** (TP 2.2×ATR / SL 1.0×ATR / timeout 900s on subsequent 15m OHLC; SL-first if both print) looks better than fixed **300s** markout — **measurement only**, not a live exit change.

**Offline result (BTC+ETH+SOL, ~700×15m, cd=900):** A TF FG=60 / 5m win **10%** avg **−11.2bps** frac_clear **0%**; B mean_revert FG=**0**; C barrier on same TF fires win **20%** avg **−9.9bps** frac_clear **13%** (exits timeout 51 / SL 7 / TP 2). None reach 0.55 — keep waiting / do not flip family or live exits from F2c alone.

| Leg | Setup | Markout |
|-----|-------|---------|
| **A** | `trend_follow` + require_4h + cooldown; **ext=0, pullback=0** | Fixed 60/300/900s fee-aware net RT |
| **B** | `mean_revert` (same cooldown) | Fixed 300s primary |
| **C** | Same TF fires as A | Barrier TP/SL/timeout (optional; vs 300s) |

```bash
# Public API only — strips OKX keys / KEEL_SKIP_DOTENV (never writes .env)
PYTHONPATH=. python scripts/okx_history_strategy_compare.py \
  --inst-ids BTC-USDT-SWAP,ETH-USDT-SWAP,SOL-USDT-SWAP \
  --bars-15m 700 --cooldown-seconds 900 \
  --json-out /tmp/keel_f2c_strategy_compare.json
```

| Piece | Location |
|-------|----------|
| Barrier exit markout | `keel.backtest.okx_history_rule.barrier_exit_markout` |
| Walk flag | `walk_forward_backtest(..., include_barrier=True)` |
| Compare CLI | `scripts/okx_history_strategy_compare.py` |

Still **E0 freeze** (no near_probe, no hurdle cut, no kill clear). Do not flip live variant or exits from F2c alone.

### Phase F0b — historical OKX candle backtest (offline)

Jo: why wait for live `post_e31` n≈4 when public candles can replay the same gates? F0b pulls OKX **public** candles (paginated), builds worker-like 15m/1H/4H snapshots, runs **TF + `require_4h` + E2B soft_tf/MACD lag + fire cooldown (900s)**, and fee-aware markout on future 15m closes (taker RT ~10 bps; funding ignored).

| Piece | Location |
|-------|----------|
| Paginated public candles | `keel.exchange.okx_public.fetch_candles` (`after`/`before`) + `fetch_candles_paginated` |
| Walk + cooldown + markout | `keel.backtest.okx_history_rule.walk_forward_backtest` |
| CLI | `scripts/okx_history_rule_backtest.py` |

```bash
# Public API only — strips OKX keys / KEEL_SKIP_DOTENV (never writes .env)
PYTHONPATH=. python scripts/okx_history_rule_backtest.py \
  --inst-ids BTC-USDT-SWAP,ETH-USDT-SWAP,SOL-USDT-SWAP \
  --bars-15m 700 --cooldown-seconds 900 --variant trend_follow \
  --json-out /tmp/keel_f0b_okx_history.json
```

**vs live post_e31 n=4:** live ledger full-gates before E3.1 are pre-4h contaminated; arming waits on a tiny post_e31 sample (`insufficient_post_e31_full_gate_sample`). F0b expands closed-bar n_steps under current E3.1 rules so fire rate + 5m netRT are measurable offline. Residuals: 15m path interpolation for 60/300s, API page limits, funding ignored, no order-book fill model.

**Still E0 freeze** (no near_probe, no hurdle cut, no kill clear, no push/merge/restart).

### Phase R4 — fee-aware Rule param suggest (offline)


Do **not** blindly set `KEEL_RULE_RSI_SHORT_MIN=40`. Instead, grid-search modest RSI / volume / `rsi_relax` knobs on the observed `okx_public` ledger cohort and keep only combos whose full fires clear the ~10 bps OKX taker round-trip fee hurdle without flooding.

```bash
# Offline suggest (local SQLite; no OKX keys; never writes .env)
PYTHONPATH=. python scripts/suggest_rule_params.py \
  --db data/keel_ledger.db --hours 168 --market-source okx_public \
  --hurdle-bps 10 --max-fire-rate 0.25 --top 5 \
  --out /tmp/keel_rule_suggest.json

# Per-instrument (BTC/ETH/SOL often differ: below_hurdle vs max_missing)
PYTHONPATH=. python scripts/suggest_rule_params.py \
  --db data/keel_ledger.db --hours 168 --market-source okx_public \
  --inst-id BTC-USDT-SWAP,ETH-USDT-SWAP,SOL-USDT-SWAP --top 3
```

**Grid (default):** `rsi_long_max ∈ {40,42,45,48}`, `rsi_short_min ∈ {52,55,58,60}`, `min_vol ∈ {0.35,0.5,0.7}`, `rsi_relax` on/off. Each combo replays `rule_based_decision` on export rows (same helpers as `compare_rule_params --db`).

**Ranking:** (1) maximize full fires with `edge_hint_bps ≥ hurdle` (default **10**), (2) keep fire rate ≤ **25%** of cohort, (3) prefer fewer `volume_ok`-only misses. Script prints top 5 + a one-line manual `.env` recommendation — **apply by hand** after review; the tool never auto-writes config. With `--inst-id` / `--all-instruments`, the grid runs **per symbol** and a combined summary ranks which instrument looks closest to fee-clearing fires.

**Interpret vs 10 bps:** treat `fires_edge>=10bps` as the fee-aware signal count. If top rows show `fires=0` or only `OVER_CAP` floods, keep observing (or widen lookback) rather than forcing `short_min=40`. Optional `--from-ledger` JSONL works the same as compare. Cross-check a candidate with:

```bash
PYTHONPATH=. python scripts/compare_rule_params.py \
  --db data/keel_ledger.db --hours 168 --market-source okx_public \
  --rsi-long-max-a 45 --rsi-short-min-a 55 --min-vol-a 0.5 \
  --rsi-long-max-b <cand_long> --rsi-short-min-b <cand_short> --min-vol-b <cand_vol>
```

---

## 相关文档

- [README.md](README.md) — 快速开始
- [SPEC.md](SPEC.md) §12 测试 / §13 demo 验收 / §15 实现顺序
- [env.example](env.example) — 环境变量模板
- `./scripts/run_acceptance.sh` — paper 自动验收（无 key）


### Live/demo market data (public candles)

On OKX REST (demo or live), each worker cycle fetches **public** 15m (and 1h) candles via `keel.exchange.okx_public.fetch_candles` — no trading permission required. Paper path keeps synthetic candles. If the public fetch fails, the cycle logs a warning, falls back to synthetic, and still completes (`data_quality_reason` starts with `synthetic_fallback:`).

Monitor/API show candle quality for live observation: Factors responses include `data_quality_reason` (ledger badge `ledger·okx` / `ledger·synth`); status `last_cycle.market_source` is `okx_public` | `synthetic` | `mixed` | `unknown`.

Kill-switch (`KEEL_KILL_SWITCH=1`) still blocks order placement; **read-only API keys are enough** for this observation path (candles + factors + decisions). Orders still need trade-enabled keys and kill-switch off.


## 观测模式（Q0 live read-only）

Kill-switch on, no orders — continuous iteration on live observation.

### One-command observe stack

```bash
./scripts/observe_up.sh      # load .env, start keel-api + keel-worker, wait /health
./scripts/observe_status.sh  # pid liveness + /health /ready snippets (no secrets)
./scripts/observe_down.sh    # stop via pid files under data/run/
```

- Scripts source repo `.env` (`set -a; . .env`), set `PYTHONPATH=.`, prefer `.venv/bin/python`.
- Pid/logs: `data/run/` (override with `KEEL_OBSERVE_RUN_DIR`, e.g. `/tmp/keel-observe`).
- Idempotent: if pidfile process is alive, print and skip duplicate start.
- **Port harden**: before start, if `:KEEL_API_PORT` (default 8080) is in use and our api pidfile is not alive → **fail** with listener identity (avoids writing a dead pid then hitting a stale `/health`). `KEEL_OBSERVE_FORCE=1` only stops PIDs recorded in observe pidfiles — **never** kills unknown listeners.
- After start: brief sleep + verify api/worker PIDs still alive; api must answer `/health` or exit non-zero with last log lines. Worker death surfaces scheduler-lock hints from the log.
- `observe_status.sh`: if `.env` is live+keys but `/health` says `environment=demo` or `/ready` has `okx_configured=false`, warn **stale API mismatch**.
- Does not disable KEEL_KILL_SWITCH (leave it 1 for read-only hanging).
- Live without OKX triple: warn only (paper fallback likely); does not hard-refuse.
- Vite Monitor is optional and separate: see frontend/README.md (dev server port 5173). Not started by observe scripts.
- Monitor UI is **中文优先** ops surface（总览 status hero / 中文 tabs）；engineer chip grids live under 「技术细节」.

### Observation checklist

1. `.env`: `KEEL_OKX_ENV=live` + read-only keys; **`KEEL_KILL_SWITCH=1`** (required for this mode).
2. Cadence: set `KEEL_OBSERVE_PRESET=fast` (300s) for denser WAIT/near-signal samples, or `default`/`slow` (900/1800). Explicit `KEEL_CYCLE_INTERVAL_SECONDS` still wins if set. Check `/api/v1/config` → `cycle_interval_seconds` + `observe_preset`.
3. Prefer `./scripts/observe_up.sh` (or manual `python -m keel.worker` / `--once`); monitor Decisions shows **near long/short** chips + missing gate names when action is WAIT (`calculus_data.signal_diag`). Optional webhook: set `KEEL_NOTIFY_WEBHOOK_URL` + `KEEL_NOTIFY_ALERTS_ONLY=1` to get only near-signal / deny / error alerts.
4. Overview **近信号雷达** card soft-fetches `GET /api/v1/signals/nearest` (WAIT / 近多 / 近空 / 已触发 + per-inst nearest/missing chips); hidden if endpoint missing.
5. Confirm no fills: kill-switch badge ON; trades empty / risk denies on any accidental BUY/SELL path.

See also §Live（无模拟盘 key） below.

## Live（无模拟盘 key）

若没有 OKX **模拟盘** API，可用 **live** 只读/观测（需你确认）：

1. `.env` 设 `KEEL_OKX_ENV=live` + 三件套 key（**勿提交**、勿贴聊天）。
2. 建议默认 `KEEL_KILL_SWITCH=1`（禁止开仓）；只读 key 本身也不能下单。
3. `python -m keel.worker --once` 应出现 `mode=okx_rest adapter=okx_rest:live:signed`。
4. CLI 会自动加载仓库根目录 `.env`（已在环境中的变量优先，不被覆盖）。
5. 真要实盘下单：另建**带交易权限**的 live key，明确关掉 kill-switch，并接受资金风险——超出当前只读验收范围。

## Q1 实盘武装（capability probe）

`GET /api/v1/status`（与 `/config`）暴露 `okx_capability`：

| 值 | 含义 |
|----|------|
| `none` | 未配置 OKX key |
| `paper` | paper 路径（不探测 OKX） |
| `read` | 账户只读可用；`orders-pending` 因权限失败 |
| `trade` | 至少 trade-read（`GET /api/v5/trade/orders-pending` 成功） |
| `error` | 探测异常（见 `okx_capability_detail`） |

探测**不会** `place_order` / cancel / close；结果缓存约 60s。Monitor Overview Credentials 卡片显示徽章（只读 / 可交易 / paper / 未知）。

**武装前提**：清 kill-switch（`KEEL_KILL_SWITCH=0`）前，先确认 `okx_capability=trade`。只读 key（`read`）不足以下单；kill-switch 行为本身不变（仍由 env 控制、门禁拒绝交易）。

### Arming checklist（只读；不自动清 kill-switch）

`GET /api/v1/status` 的 `arming` 字段汇总是否**可以**清 kill-switch（**不会**自动写 `KEEL_KILL_SWITCH=0`，也无下单）：

| 字段 | 含义 |
|------|------|
| `ready_to_arm` | `true` 仅当：keys 已配 + `okx_capability=trade` + 风险限额健全（`max_notional` / `max_daily_loss` > 0；live 另检 `KEEL_LIVE_MAX_*`）+ env 为 live/demo +（可选）近期有 shadow 排练 + **S1 经济门禁通过** |
| `kill_switch` | 当前 env 状态（echo） |
| `capability` | 同上 probe 结果 |
| `blockers` | 未就绪原因（含 shadow 排练 / **经济门禁** id，见下） |
| `warnings` | 非阻断提示（tiny equity、demo、worker_stale、market_source、默认的 shadow 排练缺失、below_hurdle 主导的 probe_skips） |
| `economic` | S1 经济门禁摘要（fills/probe/sample、net-RT win_rate、avg net-RT bps、thresholds、`passed`、可选诊断用 `by_instrument`） |

**Shadow 排练证据**：`evaluate_arming` 查 ledger 近 `KEEL_ARMING_SHADOW_HOURS`（默认 24）内是否有 `shadow_fill`。缺省 → warning `no recent shadow_fill rehearsal`；设 `KEEL_ARMING_REQUIRE_SHADOW=1` → blocker（`ready_to_arm=false`）。

### S1 经济准入门禁（read-only；默认开启）

在 checklist 之外，`ready_to_arm` 还要求近期 shadow markout **样本与质量**达标（不放宽 Rule 参数、不清 kill）：

| 门禁 | 默认 | blocker id |
|------|------|------------|
| 近 `KEEL_ARMING_ECON_HOURS`（默认 24）shadow fills ≥ `KEEL_ARMING_ECON_MIN_FILLS`（10）**或** probe fills ≥ `KEEL_ARMING_ECON_MIN_PROBE_FILLS`（5） | 见左 | `insufficient_shadow_markout_sample` |
| 300s（`KEEL_ARMING_ECON_MARKOUT_HORIZON_SECONDS`）markout `sample_count` ≥ `KEEL_ARMING_ECON_MIN_MARKOUT_SAMPLE`（5） | 见左 | 同上（**样本不足 = 未就绪，不是 pass**） |
| `probe_win_rate_net_roundtrip`（样本够时；否则 overall `win_rate_net_roundtrip`）≥ `KEEL_ARMING_ECON_MIN_PROBE_WIN_RATE_NET_RT`（0.55） | 0.55 | `probe_win_rate_net_roundtrip_below_threshold` |
| `avg_net_roundtrip_markout_bps` ≥ `KEEL_ARMING_ECON_MIN_AVG_NET_RT_BPS`（0） | 0 | `avg_net_roundtrip_markout_bps_below_threshold` |

- `KEEL_ARMING_ECON_ENABLED=0` 可关闭经济门禁（仅调试；生产保持默认 on）。
- Probe skips 以 `below_hurdle` 为主 **单独不阻断**（warning only）——说明近信号多在费率门槛下，属观察正常。
- **Kill-switch 仍须人工清除**：`ready_to_arm=true` 也绝不自动写 `KEEL_KILL_SWITCH=0`。

Monitor「实盘准入」卡展示 `economic` PASS/FAIL 与 fills/probe/mk300s/netRT 摘要（有 `by_instrument` 时附 BTC/ETH/SOL 诊断 chips）；经济 blocker 与其它 blockers 一并列出。整体门禁仍用聚合指标。

**操作步骤（人工）**：

1. 保持 `KEEL_KILL_SWITCH=1`，确认 Monitor「实盘准入」或 `arming.ready_to_arm` / `blockers` / `economic`。
2. 确认 `okx_capability=trade`（带交易权限的 live/demo key）；只读 key 停在此步。
3. 确认风险限额：`KEEL_MAX_NOTIONAL_PER_INSTRUMENT`、`KEEL_MAX_DAILY_LOSS` 已设且 > 0；live 首单另看 `KEEL_LIVE_MAX_NOTIONAL_PER_INSTRUMENT`（默认 200）与 `KEEL_LIVE_MAX_CONTRACTS_PER_INSTRUMENT`（默认 5）。
4. 阅读 `arming.warnings`（小余额、demo、行情源、worker stale、无近期 shadow_fill、below_hurdle skips）。
5. **必做影子排练（kill 可保持 ON）**：设 `KEEL_SHADOW_MODE=1` + `KEEL_KILL_SWITCH=1`（可选 `KEEL_SHADOW_NEAR_PROBE=1`），跑短暂 cycle。**kill-switch = 禁止真实下单；shadow 仍记录 `shadow_fill`**。确认 Monitor「SHADOW MODE」、ledger 有足够 shadow/probe fills，并用 `/stats/shadow_markout` 看 300s net-RT 样本，直至 `economic.passed` 且无经济 blocker。
6. **仅当** checklist + **经济门禁**绿灯、影子路径已验收、且接受资金风险时，手动设 `KEEL_KILL_SWITCH=0` 并关 `KEEL_SHADOW_MODE`（先关 shadow 再清 kill，或短暂 shadow+unkill 再关 shadow），重启相关进程——API/Monitor **无** toggle。真 live 路径自动套用更紧的 `KEEL_LIVE_MAX_*`。

### First live（Stage T gate）

产品化「能否开一笔最小实盘」只读清单——**不**清 kill、**不**下单。看 `GET /api/v1/status` 的 `first_live`（Monitor「First live」卡同步）：

| 字段 | 含义 |
|------|------|
| `allowed_now` | **仅当** kill 已关 **且** 经济门禁通过 **且** `ready_to_arm` **且** `shadow_mode` 关 → `true`；kill 开或经济未过 → **强制 false** |
| `kill_switch` / `shadow_mode` / `shadow_near_probe` / `capability` | 当前状态 echo |
| `ready_to_arm` + `blockers` + `economic` | 透传 arming / S1 摘要 |
| `suggested_live_caps` | 建议的 `KEEL_LIVE_MAX_NOTIONAL_PER_INSTRUMENT` / `KEEL_LIVE_MAX_CONTRACTS_PER_INSTRUMENT`（来自 settings） |
| `human_steps` | 英文 key 有序列表（见下） |

**`human_steps`（API keys）** → 人工含义：

1. `wait_for_economic_pass` — 等 `arming.economic.passed` / First live economic PASS（影子 markout 样本与质量达标）
2. `set_tiny_notional` — 设极小 `KEEL_LIVE_MAX_*`（默认 notional 200 / contracts 5 可再收紧）
3. `clear_kill_manually` — **仅人工**设 `KEEL_KILL_SWITCH=0`（API/Monitor **永不**自动清 kill）
4. `verify_one_fill` — 确认一笔最小实盘成交后立即停手核对
5. `re_enable_kill` — 立刻 `KEEL_KILL_SWITCH=1` 重新武装

**强调**：Keel **永不**自动写 `KEEL_KILL_SWITCH=0`，也无 UI toggle 清 kill；First live 只回答「现在能不能」，不执行。

### Shadow execution（`KEEL_SHADOW_MODE`）

可选影子成交：决策通过风控后**不**调用交易所 `place_order`，而是写入 ledger 事件 `shadow_fill`（含 decision 明细）以及可选合成 trade（`metadata.shadow=true` / `strategy_tag=keel-shadow`）。`ExecutionResult.success=true` 且 `shadow=true`。

| 项 | 行为 |
|----|------|
| 默认 | off（`KEEL_SHADOW_MODE` unset/0） |
| Kill-switch | **只拦真实下单**；`kill=1` + `shadow=1` → 仍走 shadow_fill；`kill=1` + `shadow=0` → deny |
| 暴露 | `shadow_mode` on `GET /api/v1/status` + `/config`；Monitor Overview 徽章/横幅（仅当 on） |
| Live caps | `shadow_mode=1` 时仍用 `KEEL_MAX_*`（不切 `KEEL_LIVE_MAX_*`）；真 live（live + 非 shadow）用更紧 live caps |

**在清 kill-switch 之前**先用 shadow 验收整条 decision→risk→ledger 路径（可与 kill=1 同开），确认没有真实下单。

### Q3 Near-signal shadow probe（`KEEL_SHADOW_NEAR_PROBE`）

观察窗口里 WAIT≈99%、near_signal 很多，但自然触发的 BUY/SELL 极少时，可用 **near-signal shadow probe** 把**强近信号**转成影子成交，给 arming checklist 积累 rehearsal 证据——**永不下真实单**。

| 项 | 行为 |
|----|------|
| 启用条件 | `KEEL_KILL_SWITCH=1` **且** `KEEL_SHADOW_MODE=1` **且** `KEEL_SHADOW_NEAR_PROBE=1`（缺一不可；默认 probe=off） |
| 触发 | 策略决策仍为 `WAIT`，但 `signal_diag.nearest`∈{long,short} 且 `len(missing)≤KEEL_SHADOW_NEAR_PROBE_MAX_MISSING`（默认 2，与 notify near-signal 阈值一致） |
| **Q3.4 费用门槛** | 估计 `edge_bps` 必须 ≥ hurdle；默认 hurdle = OKX `round_trip_fee_bps`（`KEEL_SHADOW_FEE_ROLE`；Regular taker→**10** / maker→**4**）。可选 `KEEL_SHADOW_NEAR_PROBE_MIN_EDGE_BPS` 覆盖；`KEEL_SHADOW_NEAR_PROBE_EDGE_MODE=round_trip\|open`（默认 round_trip）。无法估计 edge 时 **fail-closed**（跳过 probe）。审计字段：`edge_bps` / `hurdle_bps` / `fee_role` 写入 reason + shadow_fill / trade metadata |
| 动作 | 合成 `BUY_LONG`/`SELL_SHORT` → 既有 `shadow_fill` 路径；ledger `policy=shadow_near_probe` / `probe=true`；`strategy_tag=keel-shadow-near-probe` |
| Cooldown | 每 instrument `KEEL_SHADOW_NEAR_PROBE_COOLDOWN_SECONDS`（默认 900）内不重复 probe |
| 安全 | 无 kill 或无 shadow → **不** probe、**不** live order；policy 决策仍记 WAIT |
| Arming | probe 产生的 `shadow_fill` **计入** shadow 排练证据（与 forced/manual 同属 `shadow_fill`） |
| 统计 | `/stats/shadow` 含 `probe_count` + `by_policy`，可与 forced 区分；**Q3.2** 另含 `markout`；**Q3.5** 另含 `probe_skips` / `by_skip_reason`（durable `shadow_near_probe_skip`） |

**启用示例**（观察态，勿清 kill）：

```bash
# .env / process env — do not commit secrets
KEEL_KILL_SWITCH=1
KEEL_SHADOW_MODE=1
KEEL_SHADOW_NEAR_PROBE=1
# optional:
# KEEL_SHADOW_NEAR_PROBE_COOLDOWN_SECONDS=900
# KEEL_SHADOW_NEAR_PROBE_MAX_MISSING=2
# Q3.4 fee edge hurdle (unset → RT fee for KEEL_SHADOW_FEE_ROLE; 0 disables):
# KEEL_SHADOW_NEAR_PROBE_MIN_EDGE_BPS=
# KEEL_SHADOW_NEAR_PROBE_EDGE_MODE=round_trip
```

重启 worker 后看 ledger `shadow_fill`（`data.policy=shadow_near_probe`）与 `GET /api/v1/stats/shadow` 的 `probe_count`。用完将 `KEEL_SHADOW_NEAR_PROBE=0`。

Monitor / status：`GET /api/v1/status`（与 `/config`）暴露 `shadow_near_probe` + cooldown + Q3.4 `shadow_near_probe_edge_mode` / `shadow_near_probe_hurdle_bps`；Overview 显示 NEAR PROBE chip 与 quality/shadow 条的 `probe_count`。Q3.2/Q3.3：Overview soft-fail chip `mk netRT win% / ±bps`（优先 **net roundtrip**；旧 API 无 `markout`/net 字段时回退 gross 或隐藏）。

**Edge 估计（Q3.4）**：用 ATR/price×1e4 与 probe 几何（TP=2.2 ATR / SL=1.0 ATR）的粗 EV；胜率 ≈ gate 完整度（5−missing）/5 × confidence/100。与 Q3.3 `keel/exchange/okx_fees.py` 同一费率模型（makerU/takerU 或 Regular 2/5 bps）。

### Q3.5 Near-probe skip observability

When near-probe **evaluates** (kill+shadow+probe on, policy `WAIT`) but does **not** fire, the worker writes a lightweight ledger event:

```json
{
  "event_type": "shadow_near_probe_skip",
  "inst_id": "BTC-USDT-SWAP",
  "data": {
    "reason": "below_hurdle",
    "edge_bps": 4.8,
    "hurdle_bps": 10.0,
    "fee_role": "taker",
    "edge_mode": "round_trip"
  }
}
```

| `reason` | Meaning |
|----------|---------|
| `below_hurdle` | Estimated `edge_bps` &lt; fee hurdle |
| `edge_unavailable` | Edge not estimable (fail-closed) or geometry build failed |
| `cooldown` | Recent probe fill inside cooldown window |
| `max_missing` | `len(missing)` &gt; max_missing |
| `not_near` | Nearest not long/short, or confidence below min |
| `probe_disabled` | Kill/shadow/probe flag off (status/last-cycle only; **not** ledger-flooded) |

**API**: `GET /api/v1/stats/shadow?hours=N` includes:

- `probe_skips`: `{count, by_skip_reason, top_skip_reason, last_reason, last_timestamp}`
- top-level `by_skip_reason` (same map; soft-fail friendly)

Hours filter works because skips are durable ledger events. `GET /api/v1/status` → `last_cycle` also exposes cycle-local `probe_skips` / `by_skip_reason` / `top_skip_reason` / `last_probe_skip_reason` (always annotated when evaluated, even before events exist).

Monitor: optional soft chip `skip <reason> ×N` next to probe count (hidden when no skips).

```bash
curl -s "http://127.0.0.1:8080/api/v1/stats/shadow?hours=24" \
  | python -c "import sys,json; d=json.load(sys.stdin); print(d.get('probe_skips')); print(d.get('by_skip_reason'))"
```

### Q3.2 / Q3.3 Shadow markout（离线盈亏 + OKX 官方费率）

影子成交后，用 ledger 内后续 `factor_snapshots.price`（或 `decisions.entry_price`）计算简单 markout，衡量「若当时成交，稍后是否赚钱」——**只读、不下单**。Q3.3 按 OKX 官方文档扣交易费（非 naive 固定 haircut）。

```bash
curl -s "http://127.0.0.1:8080/api/v1/stats/shadow?hours=24" | python -m json.tool
# 或 sibling
curl -s "http://127.0.0.1:8080/api/v1/stats/shadow_markout?hours=24" | python -m json.tool
```

- Horizons：60s / 300s / 900s（≈1 默认 cycle）
- `BUY_LONG`：(later−fill)/fill×1e4 bps；`SELL_SHORT`：(fill−later)/fill×1e4（**gross**；字段名 `avg_markout_bps` 保持兼容）
- **Fees（USDT-margined SWAP）**：优先 live `GET /api/v5/account/trade-fee?instType=SWAP` 的 **`makerU`/`takerU`**（不是 crypto-margined `maker`/`taker`）。文档：
  - [Get fee rates](https://www.okx.com/docs-v5/en/#trading-account-rest-api-get-fee-rates)
  - [makerU/takerU 变更说明](https://www.okx.com/help/okx-will-make-changes-to-the-get-fee-rates-interface)
  - [合约手续费计算](https://www.okx.com/help/how-to-calculate-the-contract-transaction-fee) / [fee schedule](https://www.okx.com/fees)
- Fallback = OKX **Regular** USDT-margined：maker **0.0200% (2 bps)**，taker **0.0500% (5 bps)**；~1h cache；缺 key / 调用失败 soft-fail。
- `KEEL_SHADOW_FEE_ROLE=taker|maker`（默认 **taker**）；可选 `KEEL_SHADOW_MAKER_FEE_BPS` / `KEEL_SHADOW_TAKER_FEE_BPS`（`source=override`）。
- `net_open_bps = gross − open_fee_bps`；`net_roundtrip_bps = gross − 2×role`（负 maker = rebate，保留符号）。
- **Funding（独立）**：持仓跨结算才收；标准 UTC 边界常为 00/08/16。v1：fill→horizon **跨越**边界且能拉到 public `/api/v5/public/funding-rate` 才应用一次，否则 `funding_applied=false`（不编造）。短 horizon 通常为 0。参见 OKX funding FAQ / 合约费用说明。
- 缺后续价 → 计入 `skipped`，不进 sample
- Monitor：Decision quality 旁 `mk netRT …` chip（有 probe sample 时优先 net roundtrip）

