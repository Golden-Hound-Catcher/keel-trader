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

## 🚀 快速开始

### 1. 克隆仓库

```bash
git clone https://github.com/Golden-Hound-Catcher/keel-trader.git
cd keel-trader
```

### 2. 安装依赖

```bash
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
```

### 3. 配置环境变量

```bash
cp env.example .env
chmod 600 .env
```

编辑 `.env` 文件:

```bash
# OKX 环境 (demo 模拟盘 / live 实盘)
R20_OKX_ENV=demo

# OKX 凭证 (模拟盘)
OKX_DEMO_API_KEY=your_api_key
OKX_DEMO_SECRET_KEY=your_secret_key
OKX_DEMO_PASSPHRASE=your_passphrase

# LLM 配置 (OpenAI 兼容接口)
LLM_BASE_URL=https://api.openai.com/v1
LLM_API_KEY=your_llm_api_key
LLM_MODEL=gpt-4o
```

### 4. 启动服务

**方式一：只读 API 控制面**

```bash
python -m uvicorn keel.api.app:app --host 0.0.0.0 --port 8080
```

- 健康检查: `http://localhost:8080/health`
- API 文档: `http://localhost:8080/docs`

**方式二：Keel 调度器 + 控制面（推荐）**

```bash
# 终端 1：唯一调度器（paper/demo 交易循环 + 定时任务）
python -m keel.worker

# 一次性 paper/demo 垂直链路（factors→decision→risk→execution→ledger）
python -m keel.worker --once

# 终端 2：只读/管理控制面（不再自动拉起第二调度器）
python -m uvicorn r20_backend.app:app --host 0.0.0.0 --port 8080
```

> 不要同时运行 `r20_backend.scheduler`、`r20-scheduler.service` 或启用 legacy Gateway 调度。

### 5. 运行测试

```bash
# 运行所有测试
python -m pytest tests/ -v

# 只运行 Keel 核心测试
python -m pytest tests/test_keel_*.py -v
```

---

## 📁 项目结构

```
keel-trader/
├── keel/                    # 新架构核心包
│   ├── config/              # 配置管理
│   ├── domain/              # 领域模型
│   ├── exchange/            # 交易所适配器 (Protocol)
│   ├── factors/             # 技术因子 (纯函数)
│   ├── ledger/              # SQLite 账本
│   ├── llm/                 # LLM 客户端
│   ├── risk/                # 硬风控门禁
│   ├── execution/           # 执行编排
│   ├── worker/              # 调度器
│   └── api/                 # FastAPI 控制面
├── r20_backend/             # 旧版后端 (过渡期)
├── r20_gateway/             # 旧版网关 (过渡期)
├── scripts/                 # 脚本 (过渡期)
├── tests/                   # 测试
├── ARCHITECTURE.md          # 架构文档
└── README.md                # 本文件
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

### 环境变量

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

---

## 📖 架构文档

详见 [ARCHITECTURE.md](ARCHITECTURE.md)

---

## 🧪 开发

### 添加新技术因子

```python
# keel/factors/technical.py

def calculate_my_indicator(prices: list[float], period: int) -> float:
    """纯函数，可离线测试"""
    # 实现你的指标
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
        # 实现你的风控逻辑
        ...
```

---

## ⚠️ 重要提示

1. **单一调度器**: 只运行 `python -m keel.worker`；已禁用 `r20_backend.scheduler` / `r20-scheduler.service` / 后端 lifespan 自动拉起 Gateway 调度
2. **默认模拟盘**: `R20_OKX_ENV=demo` 是默认值，实盘需显式设置
3. **风控独立**: 风控门禁不可被 LLM 决策覆盖
4. **无收益承诺**: 这是研究项目，不保证任何收益

---

## 📄 开源许可

本项目基于 [MIT License](LICENSE) 开源。

---

## 致谢

本项目 fork 自 [555cute/r20-quantum-trader](https://github.com/555cute/r20-quantum-trader)，
在此基础上进行了架构重构。感谢原作者的开源贡献。
