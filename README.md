<div align="center">

# Keel Trader

### 面向 OKX 永续合约的轻量化 LLM 交易框架

**小巧核心 · 可替换适配器 · SQLite 账本 · 硬风控门禁 · 单一调度器**

[![License](https://img.shields.io/badge/license-MIT-10B981?style=flat-square)](LICENSE)
[![Python](https://img.shields.io/badge/Python-3.10%2B-3776AB?style=flat-square&logo=python&logoColor=white)](https://www.python.org/)
[![Exchange](https://img.shields.io/badge/Exchange-OKX%20V5-111827?style=flat-square)](https://www.okx.com/)

</div>

---

> [!WARNING]
> Keel Trader 是研究型开源项目，不构成投资建议，不保证任何收益。
> 强烈建议在 OKX **DEMO 模拟盘** 完成测试后，再评估是否接入实盘。

---

## 🚀 快速开始（Keel-only）

生产 / 本地开发只需两个进程：

| 进程 | 命令 | 作用 |
|------|------|------|
| **keel-api** | `uvicorn keel.api.app:app` | 只读控制面（推荐 API 入口） |
| **keel-worker** | `python -m keel.worker` | **唯一**调度器 + paper/demo 交易循环 |

支持的运行时：**keel-api** + **keel-worker** +（可选）**frontend** U1 monitor。`r20_backend` / `r20_gateway` / `r20-*.service` 均为 **legacy**（默认禁用/软拦截；见 [LEGACY.md](LEGACY.md)），勿作为新部署路径。

### 1. 克隆与安装

```bash
git clone https://github.com/Golden-Hound-Catcher/keel-trader.git
cd keel-trader
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
cp env.example .env
chmod 600 .env
```

### 2. 配置（摘要）

```bash
# Prefer KEEL_* (aliases OKX_DEMO_* / R20_OKX_ENV still work)
KEEL_OKX_ENV=demo
KEEL_OKX_API_KEY=your_api_key
KEEL_OKX_SECRET_KEY=your_secret_key
KEEL_OKX_PASSPHRASE=your_passphrase

LLM_BASE_URL=https://api.openai.com/v1
LLM_API_KEY=your_llm_api_key
LLM_MODEL=gpt-4o
```

无 OKX 密钥时 worker 使用 `PaperExchange`；配置 `KEEL_OKX_*` 后改用 `OkxRestAdapter`（demo 默认）。
Paper / 规则决策循环（`python -m keel.worker --once`）不强制需要 LLM 或交易所凭证。 决策经 `keel.policy.DecisionPolicy`（默认 `rule`；`KEEL_DECISION_POLICY=llm` 启用模块化提示词 + LLM）。

### 3. 启动（推荐）

```bash
# 终端 1：唯一调度器
python -m keel.worker

# 一次性 paper/demo 垂直链路（factors→decision→risk→execution→ledger）
python -m keel.worker --once

# 终端 2：Keel 只读 API（主入口）
python -m uvicorn keel.api.app:app --host 0.0.0.0 --port 8080
```

- 健康检查: `http://localhost:8080/health`
- API 文档: `http://localhost:8080/docs`

### 5. Phase U1 监控 UI（可选）

只读 Vue monitor：见 frontend/README.md。Vite 开发服务器代理 /api 与 /health 到 8080。路由 / 与 /monitor 为 Keel monitor。R20 Vue `/admin` 产品面已移除；管理功能延后到未来 Keel admin API（SPEC 增补）。Jinja dashboard 与 /legacy 已在 U2 移除。


### 6. 测试

```bash
make test
# 或: sh scripts/run_keel_tests.sh
# 或: python -m pytest tests/test_keel_*.py -v
```

---

## ⚠️ Legacy（非支持入口）

| 组件 | 状态 |
|------|------|
| `keel.api.app` + `keel.worker` + `frontend/` U1 | ✅ **唯一支持的运行时** |
| `r20_backend.app`（admin API 已移除，仅 410 stub） | ❌ **软拦截**：需 `KEEL_ALLOW_LEGACY_BACKEND=1`；任意路径 410 |
| `r20_gateway` | ⚠️ **legacy** 通知投递（可选）；默认无 job tick |
| `r20_backend.scheduler` / `r20-*.service` | ❌ **已禁用/门禁**（非 install 示例） |
| `scripts/ai_*_trader.py` | ⚠️ shim：默认委托 `keel.worker.cycle` |

```bash
# LEGACY ONLY — requires opt-in; do not dual-bind with keel-api
KEEL_ALLOW_LEGACY_BACKEND=1 python -m uvicorn r20_backend.app:app --host 0.0.0.0 --port 8080
```

> 不要同时运行 `r20_backend.scheduler`、`r20-*.service` 或启用 legacy Gateway 调度。详情见 [LEGACY.md](LEGACY.md)。

---

## 📁 项目结构

```
keel-trader/
├── keel/                    # 主架构（api + worker + domain）
│   ├── api/                 # FastAPI 只读控制面 ★ 推荐入口
│   ├── worker/              # 唯一调度器 + paper cycle ★
│   ├── config/ exchange/ factors/ ledger/ llm/ risk/ execution/
├── r20_backend/             # LEGACY 控制面（过渡期）
├── r20_gateway/             # LEGACY 通知（过渡期）
├── frontend/                # 监控 UI（绑定 keel.api；无 /legacy、无 /admin）
├── scripts/                 # LEGACY / shim 脚本
├── deploy/                  # systemd：仅 keel-*.service 为安装示例
├── tests/                   # 含 test_keel_*.py
├── ARCHITECTURE.md
├── STANDALONE.md
└── README.md
```

---

## 🛠️ 核心能力

| 模块 | 说明 |
|------|------|
| **交易标的** | BTC, ETH, SOL, DOGE, SUI, LINK 等 OKX USDT 永续合约 |
| **技术因子** | EMA, RSI, ATR, MACD, Bollinger Bands, VWAP, OBV |
| **LLM 决策** | OpenAI 兼容接口，严格 JSON Schema 输出 |
| **风控门禁** | 最大持仓、单日亏损、单标的保证金、冷却期、熔断开关 |
| **执行策略** | 限价优先，附带云端止盈止损 |
| **数据存储** | SQLite 追加式账本 (替代 JSON 文件) |

---

## ⚙️ 配置说明

| 变量 | 说明 | 默认值 |
|------|------|--------|
| `R20_OKX_ENV` | OKX 环境 (`demo`/`live`) | `demo` |
| `OKX_DEMO_API_KEY` | 模拟盘 API Key | - |
| `OKX_DEMO_SECRET_KEY` | 模拟盘 Secret | - |
| `OKX_DEMO_PASSPHRASE` | 模拟盘 Passphrase | - |
| `LLM_BASE_URL` | LLM API 地址 | `https://api.openai.com/v1` |
| `LLM_API_KEY` | LLM API Key | - |
| `LLM_MODEL` | LLM 模型 | `gpt-4o` |
| `KEEL_MAX_POSITIONS` | 最大持仓数 | `6` |
| `KEEL_MAX_DAILY_LOSS` | 单日最大亏损 (USDT) | `150` |
| `KEEL_MAX_ASSET_MARGIN` | 单标的最大保证金 (USDT) | `600` |
| `KEEL_API_HOST` / `KEEL_API_PORT` | Keel API 监听 | `0.0.0.0` / `8080` |

---

## 📖 文档

- [ARCHITECTURE.md](ARCHITECTURE.md) — 架构与迁移阶段
- [STANDALONE.md](STANDALONE.md) — 独立部署拓扑（Stage 3）
- [ARCHITECTURE.md](ARCHITECTURE.md) — Stage 7：legacy quarantine + DecisionPolicy
- [LEGACY.md](LEGACY.md) — `r20_*` / 旧脚本 / 旧 unit 库存与为何保留

---

## 🧪 开发

### 添加新技术因子

```python
# keel/factors/technical.py

def calculate_my_indicator(prices: list[float], period: int) -> float:
    """纯函数，可离线测试"""
    ...
```

### 添加新风控门禁

```python
# keel/risk/gates.py

class MyRiskGate(RiskGate):
    @property
    def name(self) -> str:
        return "my_gate"

    def check(self, ctx: GateContext) -> GateResult:
        ...
```

---

## ⚠️ 重要提示

1. **单一调度器**: 只运行 `python -m keel.worker`；已禁用 `r20_backend.scheduler` / `r20-scheduler.service` / 后端 lifespan 自动拉起 Gateway 调度
2. **默认 API/UI**: `keel.api.app` + U1 `frontend/` monitor；`r20_backend.app` 为已软拦截的 410 stub（admin API 已移除）
3. **默认模拟盘**: `R20_OKX_ENV=demo` 是默认值，实盘需显式设置
4. **风控独立**: 风控门禁不可被 LLM 决策覆盖
5. **无收益承诺**: 这是研究项目，不保证任何收益

---

## 📄 开源许可

本项目基于 [MIT License](LICENSE) 开源。

---

## 致谢

本项目 fork 自 [555cute/r20-quantum-trader](https://github.com/555cute/r20-quantum-trader)，
在此基础上进行了架构重构。感谢原作者的开源贡献。
