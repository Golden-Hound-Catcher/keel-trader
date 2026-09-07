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


## 7. Decision quality / P2 observability

Read-only decision stats (no HTTP writes):

```bash
# After paper/demo cycles have written the ledger
curl -s "http://127.0.0.1:8080/api/v1/stats/decisions?hours=24" | python -m json.tool
```

Response fields: `decision_count`, `by_action`, `by_policy`, `wait_rate` (0–1), `risk_deny_events` (`risk_gate_blocked` count), `cycle_count` (`worker_cycle_summary`), `avg_cycle_duration_ms`.

Monitor Overview soft-fetches the same endpoint (card hidden if API missing). Decisions table shows `policy_name` (modules truncated in title).

Offline policy compare (no OKX keys):

```bash
PYTHONPATH=. python scripts/compare_policies_paper.py
# prints action histogram for stub and rule; exit 0
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
| `ready_to_arm` | `true` 仅当：keys 已配 + `okx_capability=trade` + 风险限额健全（`max_notional` / `max_daily_loss` > 0；live 另检 `KEEL_LIVE_MAX_*`）+ env 为 live/demo +（可选）近期有 shadow 排练 |
| `kill_switch` | 当前 env 状态（echo） |
| `capability` | 同上 probe 结果 |
| `blockers` | 未就绪的人类可读原因（含 `KEEL_ARMING_REQUIRE_SHADOW=1` 时的 `no recent shadow_fill rehearsal`） |
| `warnings` | 非阻断提示（tiny equity、demo、worker_stale、market_source、默认的 shadow 排练缺失） |

**Shadow 排练证据**：`evaluate_arming` 查 ledger 近 `KEEL_ARMING_SHADOW_HOURS`（默认 24）内是否有 `shadow_fill`。缺省 → warning `no recent shadow_fill rehearsal`；设 `KEEL_ARMING_REQUIRE_SHADOW=1` → blocker（`ready_to_arm=false`）。

**操作步骤（人工）**：

1. 保持 `KEEL_KILL_SWITCH=1`，确认 Monitor「实盘准入」或 `arming.ready_to_arm` / `blockers`。
2. 确认 `okx_capability=trade`（带交易权限的 live/demo key）；只读 key 停在此步。
3. 确认风险限额：`KEEL_MAX_NOTIONAL_PER_INSTRUMENT`、`KEEL_MAX_DAILY_LOSS` 已设且 > 0；live 首单另看 `KEEL_LIVE_MAX_NOTIONAL_PER_INSTRUMENT`（默认 200）与 `KEEL_LIVE_MAX_CONTRACTS_PER_INSTRUMENT`（默认 5）。
4. 阅读 `arming.warnings`（小余额、demo、行情源、worker stale、无近期 shadow_fill）。
5. **必做影子排练（kill 可保持 ON）**：设 `KEEL_SHADOW_MODE=1` + `KEEL_KILL_SWITCH=1`，跑短暂 cycle。**kill-switch = 禁止真实下单；shadow 仍记录 `shadow_fill`**。确认 Monitor「SHADOW MODE」、`shadow_mode=true`、ledger 有 `shadow_fill` 且无 OKX `place_order`，直至 arming 不再警告/阻断 shadow 排练。
6. **仅当** checklist 绿灯、影子路径已验收、且接受资金风险时，手动设 `KEEL_KILL_SWITCH=0` 并关 `KEEL_SHADOW_MODE`（先关 shadow 再清 kill，或短暂 shadow+unkill 再关 shadow），重启相关进程——API/Monitor **无** toggle。真 live 路径自动套用更紧的 `KEEL_LIVE_MAX_*`。

### Shadow execution（`KEEL_SHADOW_MODE`）

可选影子成交：决策通过风控后**不**调用交易所 `place_order`，而是写入 ledger 事件 `shadow_fill`（含 decision 明细）以及可选合成 trade（`metadata.shadow=true` / `strategy_tag=keel-shadow`）。`ExecutionResult.success=true` 且 `shadow=true`。

| 项 | 行为 |
|----|------|
| 默认 | off（`KEEL_SHADOW_MODE` unset/0） |
| Kill-switch | **只拦真实下单**；`kill=1` + `shadow=1` → 仍走 shadow_fill；`kill=1` + `shadow=0` → deny |
| 暴露 | `shadow_mode` on `GET /api/v1/status` + `/config`；Monitor Overview 徽章/横幅（仅当 on） |
| Live caps | `shadow_mode=1` 时仍用 `KEEL_MAX_*`（不切 `KEEL_LIVE_MAX_*`）；真 live（live + 非 shadow）用更紧 live caps |

**在清 kill-switch 之前**先用 shadow 验收整条 decision→risk→ledger 路径（可与 kill=1 同开），确认没有真实下单。

