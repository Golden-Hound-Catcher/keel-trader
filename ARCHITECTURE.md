# Keel Trader Architecture

> 船舶龙骨：小而稳固的结构核心，可替换的适配器

## 设计原则

1. **单一调度器**：只有一个进程拥有调度权限，使用文件锁防止重复运行
2. **类型化交易所接口**：通过 Protocol 定义接口，不在核心路径使用 shell CLI
3. **SQLite 账本**：使用 SQLite 替代 JSON 文件作为进程间通信
4. **硬风控门禁**：风控规则独立于 LLM，不可被 AI 决策覆盖
5. **纯函数因子**：技术指标是可离线测试的纯函数

## 包结构

```
keel/
├── __init__.py          # 版本信息
├── config/              # 配置管理
│   ├── __init__.py
│   └── settings.py      # 集中式配置，支持环境变量
├── domain/              # 核心领域模型
│   ├── __init__.py
│   └── instruments.py   # 交易标的定义
├── exchange/            # 交易所适配器
│   ├── __init__.py
│   ├── protocol.py      # 类型化 Protocol 接口
│   ├── okx_rest.py      # OKX REST API 适配器
│   ├── okx_rest.py      # OKX V5 REST（OkxRestAdapter）
│   ├── factory.py       # build_exchange：OKX keys → REST else paper
│   └── paper.py         # PaperExchange 模拟交易适配器
├── factors/             # 技术因子计算
│   ├── __init__.py
│   ├── market_data.py   # 行情数据结构
│   └── technical.py     # 纯函数技术指标
├── ledger/              # SQLite 账本
│   ├── __init__.py
│   └── sqlite_ledger.py # 追加式账本
├── llm/                 # LLM 决策集成
│   ├── __init__.py
│   └── client.py        # OpenAI 兼容客户端
├── risk/                # 风险控制
│   ├── __init__.py
│   └── gates.py         # 硬风控门禁
├── execution/           # 执行编排
│   ├── __init__.py
│   └── orchestrator.py  # 决策→风控→下单流程
├── worker/              # 工作进程
│   ├── __init__.py
│   └── scheduler.py     # 单一调度器
└── api/                 # API 控制面
    ├── __init__.py
    ├── app.py           # FastAPI 入口
    └── routers/         # 路由模块
        ├── health.py
        ├── status.py
        ├── positions.py
        ├── decisions.py
        └── factors.py
```

## 数据流

```
┌─────────────────────────────────────────────────────────────────┐
│                        Keel Scheduler                            │
│  (单一所有权，文件锁，15分钟循环)                                 │
└─────────────────────┬───────────────────────────────────────────┘
                      │
                      ▼
┌─────────────────────────────────────────────────────────────────┐
│                     Market Data Fetch                            │
│  OKX REST API → factors/technical.py → MarketSnapshot            │
└─────────────────────┬───────────────────────────────────────────┘
                      │
                      ▼
┌─────────────────────────────────────────────────────────────────┐
│                      LLM Decision                                │
│  llm/client.py → OpenAI 兼容接口 → JSON Schema 验证              │
└─────────────────────┬───────────────────────────────────────────┘
                      │
                      ▼
┌─────────────────────────────────────────────────────────────────┐
│                    Risk Gate Check                               │
│  risk/gates.py (硬门禁，不可覆盖)                                │
│  - 最大持仓数                                                    │
│  - 单日亏损限额                                                  │
│  - 单标的保证金上限                                              │
│  - 止损冷却期                                                    │
│  - 紧急熔断开关                                                  │
└─────────────────────┬───────────────────────────────────────────┘
                      │ (全部通过才继续)
                      ▼
┌─────────────────────────────────────────────────────────────────┐
│                 Execution Orchestrator                           │
│  execution/orchestrator.py                                       │
│  - 计算仓位大小                                                  │
│  - 提交限价单 + TP/SL                                            │
│  - 验证交易所确认                                                │
└─────────────────────┬───────────────────────────────────────────┘
                      │
                      ▼
┌─────────────────────────────────────────────────────────────────┐
│                   SQLite Ledger                                  │
│  ledger/sqlite_ledger.py                                         │
│  - trades 表：交易记录                                           │
│  - decisions 表：AI 决策记录                                     │
│  - events 表：通用事件日志                                       │
└─────────────────────────────────────────────────────────────────┘
```

## 扩展点

### 1. 添加新交易所

实现 `exchange/protocol.py` 中的 `ExchangeProtocol`:

```python
class MyExchangeAdapter:
    def get_balance(self) -> AccountBalance: ...
    def get_positions(self) -> list[Position]: ...
    def place_order(self, request: OrderRequest) -> OrderResult: ...
    # ... 其他方法
```

### 2. 添加新技术因子

在 `factors/technical.py` 中添加纯函数:

```python
def calculate_my_indicator(prices: list[float], period: int) -> float:
    """纯函数，可离线单元测试"""
    ...
```

### 3. 添加新风控门禁

继承 `risk/gates.py` 中的 `RiskGate`:

```python
class MyRiskGate(RiskGate):
    @property
    def name(self) -> str:
        return "my_gate"
    
    def check(self, ctx: GateContext) -> GateResult:
        if some_condition:
            return GateResult(passed=False, gate_name=self.name, reason="...")
        return GateResult(passed=True, gate_name=self.name)
```

### 4. 自定义 LLM Provider

配置环境变量:

```bash
LLM_BASE_URL=https://your-provider/v1
LLM_API_KEY=your-key
LLM_MODEL=your-model
```

## 调度器所有权

**重要**：只有一个调度器可以运行。

| 组件 | 状态 | 说明 |
|------|------|------|
| `keel/worker` (`python -m keel.worker`) | ✅ 唯一调度器 | Stage 2  sole owner |
| `keel/worker/cycle.py` | ✅ paper/demo 垂直链路 | factors→decision→risk→execution→ledger |
| `r20_gateway/scheduler.py` | ❌ 默认禁用 | 仅紧急回滚 `KEEL_ENABLE_LEGACY_GATEWAY_SCHEDULER=1` |
| `r20_backend/scheduler.py` | ❌ 硬退出 | 调用即失败 |
| `deploy/r20-scheduler.service` | ❌ 禁用 | 改用 `deploy/keel-worker.service` |

## 配置

所有配置通过环境变量:

```bash
# OKX（优先 KEEL_*；有密钥 → OkxRestAdapter，否则 PaperExchange）
KEEL_OKX_ENV=demo
KEEL_OKX_API_KEY=xxx
KEEL_OKX_SECRET_KEY=xxx
KEEL_OKX_PASSPHRASE=xxx

# LLM（可选；paper 规则决策不需要）
KEEL_LLM_API_KEY=xxx          # 或 LLM_API_KEY
LLM_BASE_URL=https://api.openai.com/v1
LLM_MODEL=gpt-4o

# 风控参数
KEEL_MAX_POSITIONS=6
KEEL_MAX_DAILY_LOSS=150
KEEL_MAX_ASSET_MARGIN=600
```

## 迁移路径

从 R20 迁移到 Keel:

1. **阶段 1**: Keel 模块作为新代码的主入口
2. **阶段 2**: 旧脚本通过 shim 调用 Keel 模块
3. **阶段 3**: 移除旧代码，只保留 Keel

当前状态: **阶段 5** - OKX demo REST 适配器（`OkxRestAdapter`）+ 配置收敛；无密钥时仍走 `PaperExchange`；`r20_*` 保持 legacy

## 测试

```bash
# 运行 Keel 单元测试
python -m pytest tests/test_keel_*.py -v

# 运行所有测试
python -m pytest tests/ -v
```

## Stage 4（执行路径硬化）

- Decision 几何 / RR 校验统一为 `keel.llm.client.validate_decision`
- `ExecutionOrchestrator`：fill → trade；paper resting / risk / invalid → ledger events
- Worker cycle 写入 `factor_snapshots`；`keel.api` 默认从 SQLite 读 decisions/trades/events/factors
- 公共行情助手：`keel.exchange.okx_public`（无 shell CLI）
- 配置：`KEEL_OKX_ENV` / `KEEL_LEDGER_DB` + 精简 `env.example`


## Stage 5（OKX demo REST 适配器）

- `keel.exchange.OkxRestAdapter`：OKX V5 REST（有密钥签名；公开行情可无密钥）
- `keel.exchange.build_exchange()`：密钥齐全 → OKX REST，否则 → `PaperExchange`
- Worker cycle 记录 `adapter` / `mode`；CI 用 mock HTTP，不依赖外网
- 配置单一入口：`keel.config.settings`（`KEEL_OKX_*` + `KEEL_LLM_*` / 兼容别名）
- 仍不扩展 Vue / QQ / council / backup；不批量删除 `r20_*`

## 入口点（Stage 3 唯一推荐）

```bash
# 启动 API (只读控制面) — 主入口 keel.api.app
python -m uvicorn keel.api.app:app --host 0.0.0.0 --port 8080

# 启动唯一 Worker / 调度器
python -m keel.worker

# 单次 paper/demo 循环
python -m keel.worker --once

# 测试
make test
```

`r20_backend.app` / dashboard 仅为 **legacy 只读 UI**，勿作为新部署默认 API。
`r20_backend.scheduler` 与 `r20-scheduler.service` 已禁用。

## 免责声明

Keel Trader 是研究型开源项目，不构成投资建议，不保证任何收益。
强烈建议在 OKX DEMO 模拟盘完成测试后，再评估是否接入实盘。

## License

MIT
