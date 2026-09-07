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

Quality scorecard (`/stats/quality`): single glance for observe health — `market_source` breakdown (`okx_public` / `synthetic` / `unknown`), `decision_count`, `wait_rate`, `by_action`, `near_signal_rate` (fraction of WAIT with `signal_diag.nearest` in `{long,short}`), nested `shadow` (`count` / `by_action` / `last_timestamp`), `cycle_count`, `avg_cycle_duration_ms`. Read-only; does not enable trading.

Monitor Overview soft-fetches decisions + shadow stats + quality scorecard chips (wait / near / shadow / okx share; hidden if API missing). Decisions table shows `policy_name` and `calculus_data.market_source` chip when present.

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

### Phase R4 — fee-aware Rule param suggest (offline)

Do **not** blindly set `KEEL_RULE_RSI_SHORT_MIN=40`. Instead, grid-search modest RSI / volume / `rsi_relax` knobs on the observed `okx_public` ledger cohort and keep only combos whose full fires clear the ~10 bps OKX taker round-trip fee hurdle without flooding.

```bash
# Offline suggest (local SQLite; no OKX keys; never writes .env)
PYTHONPATH=. python scripts/suggest_rule_params.py \
  --db data/keel_ledger.db --hours 168 --market-source okx_public \
  --hurdle-bps 10 --max-fire-rate 0.25 --top 5 \
  --out /tmp/keel_rule_suggest.json
```

**Grid (default):** `rsi_long_max ∈ {40,42,45,48}`, `rsi_short_min ∈ {52,55,58,60}`, `min_vol ∈ {0.35,0.5,0.7}`, `rsi_relax` on/off. Each combo replays `rule_based_decision` on export rows (same helpers as `compare_rule_params --db`).

**Ranking:** (1) maximize full fires with `edge_hint_bps ≥ hurdle` (default **10**), (2) keep fire rate ≤ **25%** of cohort, (3) prefer fewer `volume_ok`-only misses. Script prints top 5 + a one-line manual `.env` recommendation — **apply by hand** after review; the tool never auto-writes config.

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
| `economic` | S1 经济门禁摘要（fills/probe/sample、net-RT win_rate、avg net-RT bps、thresholds、`passed`） |

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

Monitor「实盘准入」卡展示 `economic` PASS/FAIL 与 fills/probe/mk300s/netRT 摘要；经济 blocker 与其它 blockers 一并列出。

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

