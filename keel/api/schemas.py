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
    seconds_since_last_cycle: int | None = None
    worker_stale: bool = False


class CredentialsStatus(BaseModel):
    okx: bool
    llm: bool


class RiskDenyReason(BaseModel):
    """One risk-gate deny captured in a worker cycle summary (capped list)."""

    gate: str
    reason: str = ""


class CycleError(BaseModel):
    """One non-risk instrument error from a worker cycle summary."""

    inst_id: str | None = None
    error: str


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
    error_count: int = 0
    errors: list[CycleError] = Field(default_factory=list)
    policy_success: bool | None = None
    duration_ms: int = 0
    # Candle quality aggregate for this cycle (okx_public | synthetic | mixed | unknown).
    market_source: str | None = None


class ArmingStatus(BaseModel):
    """Q1 read-only arming checklist (never writes KEEL_KILL_SWITCH)."""

    ready_to_arm: bool = False
    kill_switch: bool = False
    capability: str = "none"
    blockers: list[str] = Field(default_factory=list)
    warnings: list[str] = Field(default_factory=list)


class StatusResponse(BaseModel):
    version: str
    mode: str
    uptime_seconds: int
    environment: str
    credentials: CredentialsStatus
    ledger_db: str
    kill_switch: bool = False
    shadow_mode: bool = False
    # Q3.1: near-signal shadow probe flag + cooldown (same as /config; never live orders).
    shadow_near_probe: bool = False
    shadow_near_probe_cooldown_seconds: int = 900
    decision_policy: str = "rule"
    last_cycle: LastCycleSummary | None = None
    seconds_since_last_cycle: int | None = None
    worker_stale: bool = False
    # Q1: OKX key capability (none|paper|read|trade|error); never places orders.
    okx_capability: str = "none"
    okx_capability_detail: str | None = None
    # Q1: read-only arming checklist (operator still flips KEEL_KILL_SWITCH manually).
    arming: ArmingStatus | None = None


class ConfigResponse(BaseModel):
    environment: str
    max_positions: int
    max_daily_loss: float
    max_asset_margin: float
    max_notional_per_instrument: float = 2000.0
    max_contracts_per_instrument: int = 50
    # First-live tighter caps (KEEL_LIVE_MAX_*); paper/shadow keep KEEL_MAX_*.
    live_max_notional_per_instrument: float = 200.0
    live_max_contracts_per_instrument: int = 5
    # Caps actually applied by risk gates right now.
    effective_max_notional_per_instrument: float = 2000.0
    effective_max_contracts_per_instrument: int = 50
    llm_model: str
    kill_switch: bool = False
    shadow_mode: bool = False
    # Q3 near-signal shadow probe (kill+shadow only; never live orders).
    shadow_near_probe: bool = False
    shadow_near_probe_cooldown_seconds: int = 900
    shadow_near_probe_max_missing: int = 2
    decision_policy: str = "rule"
    instruments: list[str] = Field(default_factory=list)
    notify_configured: bool = False
    notify_alerts_only: bool = False
    notify_format: str = "keel"
    exchange_mode: str = "paper"
    cycle_interval_seconds: int = 900
    observe_preset: str | None = None
    scheduler_jobs: list[str] = Field(default_factory=lambda: ["trader"])
    # Q1: same capability label as status (non-secret).
    okx_capability: str = "none"
    okx_capability_detail: str | None = None


class DailyPnlResponse(BaseModel):
    date: str
    realized_pnl: float
    source: str = "ledger"


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
    signal_diag: dict[str, Any] | None = None
    policy_name: str = ""
    prompt_modules: list[str] | None = None


class DecisionsResponse(BaseModel):
    count: int
    decisions: list[DecisionItem]


class DecisionStatsResponse(BaseModel):
    """Aggregated decision quality / observability stats (read-only)."""

    hours: int
    decision_count: int
    by_action: dict[str, int] = Field(default_factory=dict)
    by_policy: dict[str, int] = Field(default_factory=dict)
    wait_rate: float = 0.0
    risk_deny_events: int = 0
    cycle_count: int = 0
    avg_cycle_duration_ms: float | None = None
    market_source: str = "any"


class ShadowMarkoutActionStats(BaseModel):
    """Per-action markout aggregates within one horizon."""

    sample_count: int = 0
    # Gross mid markout (fee-unaware; back-compat).
    avg_markout_bps: float | None = None
    median_markout_bps: float | None = None
    win_rate: float | None = None
    # Q3.3 fee-aware nets.
    avg_net_open_markout_bps: float | None = None
    median_net_open_markout_bps: float | None = None
    win_rate_net_open: float | None = None
    avg_net_roundtrip_markout_bps: float | None = None
    median_net_roundtrip_markout_bps: float | None = None
    win_rate_net_roundtrip: float | None = None


class ShadowMarkoutHorizon(BaseModel):
    """Markout outcome stats for one wall-clock horizon after fill."""

    horizon_seconds: int
    sample_count: int = 0
    skipped: int = 0
    # Gross (back-compat name avg_markout_bps).
    avg_markout_bps: float | None = None
    median_markout_bps: float | None = None
    win_rate: float | None = None
    avg_net_open_markout_bps: float | None = None
    median_net_open_markout_bps: float | None = None
    win_rate_net_open: float | None = None
    avg_net_roundtrip_markout_bps: float | None = None
    median_net_roundtrip_markout_bps: float | None = None
    win_rate_net_roundtrip: float | None = None
    probe_sample_count: int = 0
    probe_avg_markout_bps: float | None = None
    probe_median_markout_bps: float | None = None
    probe_win_rate: float | None = None
    probe_avg_net_open_markout_bps: float | None = None
    probe_median_net_open_markout_bps: float | None = None
    probe_win_rate_net_open: float | None = None
    probe_avg_net_roundtrip_markout_bps: float | None = None
    probe_median_net_roundtrip_markout_bps: float | None = None
    probe_win_rate_net_roundtrip: float | None = None
    funding_applied_count: int = 0
    by_action: dict[str, ShadowMarkoutActionStats] = Field(default_factory=dict)


class ShadowMarkoutBlock(BaseModel):
    """Offline shadow fill markout nest (factor_snapshots / entry_price)."""

    price_source: str = "factor_snapshots"
    horizons: list[ShadowMarkoutHorizon] = Field(default_factory=list)


class ShadowFeeModel(BaseModel):
    """OKX-official fee model used for net markout (Q3.3)."""

    source: str = "fallback"  # live|fallback|override
    inst_type: str = "SWAP"
    margin: str = "USDT"
    level: str | None = None
    maker_bps: float = 2.0
    taker_bps: float = 5.0
    role: str = "taker"
    open_fee_bps: float = 5.0
    round_trip_fee_bps: float = 10.0
    funding_note: str = ""
    funding_applied: bool = False


class ShadowStatsResponse(BaseModel):
    """Aggregated shadow_fill rehearsal stats (read-only)."""

    hours: int
    count: int = 0
    by_action: dict[str, int] = Field(default_factory=dict)
    by_policy: dict[str, int] = Field(default_factory=dict)
    probe_count: int = 0
    last_timestamp: float | None = None
    # Q3.3: OKX fee model for net markout (soft-fail if older clients ignore).
    fee_model: ShadowFeeModel | None = None
    # Q3.2: optional markout nest (absent on older builds / soft-fail clients).
    markout: ShadowMarkoutBlock | None = None


class QualityShadowBlock(BaseModel):
    """Nested shadow_fill summary inside the quality scorecard."""

    count: int = 0
    by_action: dict[str, int] = Field(default_factory=dict)
    by_policy: dict[str, int] = Field(default_factory=dict)
    probe_count: int = 0
    last_timestamp: float | None = None


class QualityStatsResponse(BaseModel):
    """Compact observation quality scorecard (read-only)."""

    hours: int
    market_source: dict[str, int] = Field(default_factory=dict)
    decision_count: int = 0
    wait_rate: float = 0.0
    by_action: dict[str, int] = Field(default_factory=dict)
    near_signal_rate: float = 0.0
    shadow: QualityShadowBlock = Field(default_factory=QualityShadowBlock)
    cycle_count: int = 0
    avg_cycle_duration_ms: float | None = None


class NearestSignalItem(BaseModel):
    """Latest per-instrument decision with signal_diag radar fields."""

    inst_id: str
    action: str
    timestamp: float
    nearest: str | None = None
    missing: list[str] = Field(default_factory=list)
    rsi_14: float | None = None
    trend_15m: str | None = None
    volume_ratio: float | None = None
    ema_9: float | None = None
    ema_21: float | None = None
    macd_histogram: float | None = None


class NearestSignalsSummary(BaseModel):
    waiting: int = 0
    long_nearest: int = 0
    short_nearest: int = 0
    fired_long: int = 0
    fired_short: int = 0


class NearestSignalsResponse(BaseModel):
    """Q0 near-signal radar: latest decision per watch instrument (read-only)."""

    hours: int
    count: int
    summary: NearestSignalsSummary
    signals: list[NearestSignalItem]


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
    # Worker snapshot quality tag (okx_public / synthetic / synthetic_fallback:…); live path = okx_public.
    data_quality_reason: str | None = None
