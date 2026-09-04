"""
Pydantic response models for Keel API — keeps OpenAPI honest.

Routers return these models (or containers of them) instead of ad-hoc dicts.
"""
from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field


class HealthResponse(BaseModel):
    status: str
    service: str
    version: str
    timestamp: int
    environment: Literal["demo", "live"]


class ReadyResponse(BaseModel):
    ready: bool
    okx_configured: bool
    llm_configured: bool


class CredentialsStatus(BaseModel):
    okx: bool
    llm: bool


class RiskDenyReason(BaseModel):
    """One risk-gate deny captured in a worker cycle summary (capped list)."""

    gate: str
    reason: str = ""


class LastCycleSummary(BaseModel):
    """Structured summary of the most recent keel.worker.cycle run."""

    timestamp: float
    mode: str
    adapter: str = ""
    policy: str = ""
    instruments: int = 0
    decision_counts: dict[str, int] = Field(default_factory=dict)
    risk_denies: int = 0
    risk_deny_reasons: list[RiskDenyReason] = Field(default_factory=list)
    errors: list[dict[str, Any]] = Field(default_factory=list)
    policy_success: bool | None = None
    duration_ms: int = 0


class StatusResponse(BaseModel):
    version: str
    mode: str
    uptime_seconds: int
    environment: str
    credentials: CredentialsStatus
    ledger_db: str
    kill_switch: bool = False
    last_cycle: LastCycleSummary | None = None


class ConfigResponse(BaseModel):
    environment: str
    max_positions: int
    max_daily_loss: float
    max_asset_margin: float
    llm_model: str
    kill_switch: bool = False


class PositionItem(BaseModel):
    inst_id: str
    side: str
    size: float
    avg_price: float
    mark_price: float
    upl: float
    upl_ratio: float
    leverage: float
    margin: float


class PositionsResponse(BaseModel):
    count: int
    positions: list[PositionItem]
    source: Literal["okx", "paper"]


class BalanceResponse(BaseModel):
    total_equity: float
    available: float
    cash: float
    unrealized_pnl: float
    margin_usage_pct: float
    source: Literal["okx", "paper"]


class DecisionItem(BaseModel):
    id: int | None = None
    timestamp: float
    inst_id: str
    action: str
    confidence: float
    entry_price: float | None = None
    take_profit: float | None = None
    stop_loss: float | None = None
    reason: str = ""
    calculus_data: dict[str, Any] | None = None


class DecisionsResponse(BaseModel):
    count: int
    decisions: list[DecisionItem]


class LatestDecisionResponse(BaseModel):
    found: bool
    inst_id: str | None = None
    decision: DecisionItem | None = None


class TradeItem(BaseModel):
    id: int | None = None
    timestamp: float
    inst_id: str
    action: str
    direction: str
    size: float
    price: float
    pnl: float | None = None
    strategy_tag: str = ""
    reason: str = ""
    metadata: dict[str, Any] | None = None


class TradesResponse(BaseModel):
    count: int
    trades: list[TradeItem]


class EventItem(BaseModel):
    id: int | None = None
    timestamp: float
    event_type: str
    inst_id: str | None = None
    data: dict[str, Any] | None = None


class EventsResponse(BaseModel):
    count: int
    events: list[EventItem]


class MacdBlock(BaseModel):
    line: float
    signal: float
    histogram: float


class BollingerBlock(BaseModel):
    middle: float
    upper: float
    lower: float
    bandwidth: float
    percent_b: float


class FactorsResponse(BaseModel):
    model_config = ConfigDict(extra="ignore")

    inst_id: str
    source: Literal["ledger", "okx_public"]
    price: float
    ema_9: float
    ema_21: float
    ema_55: float = 0.0
    rsi_14: float
    rsi_7: float = 0.0
    atr_14: float
    macd: MacdBlock
    timestamp: float | None = None
    trend_15m: str | None = None
    volume_ratio: float | None = None
    bollinger: BollingerBlock | None = None
    candle_count: int | None = None
