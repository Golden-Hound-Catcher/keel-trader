"""
Ledger / audit domain records shared by policy, execution, ledger, and API.

These are the persistence-oriented shapes (dataclass). Wire/OpenAPI shapes live
in ``keel.api.schemas`` and are built from these records.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone, timedelta
from typing import Any, Literal

BJ_TZ = timezone(timedelta(hours=8))


@dataclass
class TradeRecord:
    """Immutable trade record for the ledger."""

    id: int | None = None
    timestamp: float = 0.0
    inst_id: str = ""
    action: Literal["open", "close", "scale_in"] = "open"
    direction: Literal["long", "short"] = "long"
    size: float = 0.0
    price: float = 0.0
    pnl: float | None = None
    fee: float = 0.0
    strategy_tag: str = ""
    reason: str = ""
    metadata: dict[str, Any] | None = None

    @property
    def time_str(self) -> str:
        return datetime.fromtimestamp(self.timestamp, tz=BJ_TZ).strftime("%Y-%m-%d %H:%M:%S")


@dataclass
class DecisionRecord:
    """Persisted AI / paper decision for audit trail."""

    id: int | None = None
    timestamp: float = 0.0
    inst_id: str = ""
    action: str = "WAIT"
    confidence: float = 0.0
    entry_price: float | None = None
    take_profit: float | None = None
    stop_loss: float | None = None
    reason: str = ""
    calculus_data: dict[str, Any] | None = None
    raw_response: str | None = None


@dataclass
class FactorSnapshot:
    """Technical factor snapshot persisted after each cycle."""

    id: int | None = None
    timestamp: float = 0.0
    inst_id: str = ""
    price: float = 0.0
    rsi_14: float = 0.0
    ema_9: float = 0.0
    ema_21: float = 0.0
    atr_14: float = 0.0
    macd_histogram: float = 0.0
    trend_15m: str = "neutral"
    volume_ratio: float = 1.0
    payload: dict[str, Any] | None = None


@dataclass
class LedgerEvent:
    """Generic ledger event (risk blocks, fills, cycle completes)."""

    id: int | None = None
    timestamp: float = 0.0
    event_type: str = ""
    inst_id: str | None = None
    data: dict[str, Any] | None = None
