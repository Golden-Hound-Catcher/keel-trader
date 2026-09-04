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
| `KEEL_MAX_NOTIONAL_PER_INSTRUMENT` | 单标的名义价值上限（默认 2000 USDT） |
| `KEEL_MAX_CONTRACTS_PER_INSTRUMENT` | 单标的合约张数上限（默认 50） |
| `KEEL_MAX_POSITIONS` / `KEEL_MAX_DAILY_LOSS` / `KEEL_MAX_ASSET_MARGIN` | 持仓数、日亏、单标保证金门禁 |

配置摘要见 `GET /api/v1/config`。

---


## 4b. 周期通知 webhooks / Cycle notify

| 变量 | 默认 | 说明 |
|------|------|------|
| `KEEL_NOTIFY_WEBHOOK_URL` | 空 | 空 → NullNotifier（**无网络**）；非空 → 每轮 POST |
| `KEEL_NOTIFY_ALERTS_ONLY` | `0` | `1` 时仅当 `alert=true`（`ok` 假 / `risk_denies>0` / `error_count>0`）才发送 |
| `KEEL_NOTIFY_FORMAT` | `keel` | `keel` → `{"event","payload"}`；`discord` → `{"content": text}`（≤1900 字符，无需桥接） |

Payload 含 `risk_denies` / `risk_deny_reasons`（capped）、`error_count` / `errors`（capped）、`duration_ms`、`alert`、`severity`（`ok`\|`warn`\|`error`）、人类可读 `text`。非密钥字段亦在 `GET /api/v1/config`（`notify_configured` / `notify_alerts_only` / `notify_format`）。

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

## 相关文档

- [README.md](README.md) — 快速开始
- [SPEC.md](SPEC.md) §12 测试 / §13 demo 验收 / §15 实现顺序
- [env.example](env.example) — 环境变量模板
- `./scripts/run_acceptance.sh` — paper 自动验收（无 key）
