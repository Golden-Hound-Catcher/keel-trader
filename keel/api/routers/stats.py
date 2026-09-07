"""Read-only decision / cycle observability stats."""
from __future__ import annotations

from typing import Literal

from fastapi import APIRouter, Query

from keel.api.deps import get_ledger
from keel.api.schemas import (
    DecisionStatsResponse,
    QualityShadowBlock,
    QualityStatsResponse,
    ShadowStatsResponse,
)

router = APIRouter()


@router.get("/stats/decisions", response_model=DecisionStatsResponse)
def get_decision_stats(
    hours: int = Query(default=24, ge=1, le=168),
    market_source: Literal["okx_public", "synthetic", "any"] = Query(default="any"),
) -> DecisionStatsResponse:
    """Aggregate decision quality stats over the last ``hours`` (max 168)."""
    ledger = get_ledger()
    raw = ledger.get_decision_stats(
        hours=float(hours),
        market_source=market_source,
    )
    return DecisionStatsResponse(
        hours=hours,
        decision_count=int(raw.get("decision_count", 0)),
        by_action=dict(raw.get("by_action") or {}),
        by_policy=dict(raw.get("by_policy") or {}),
        wait_rate=float(raw.get("wait_rate") or 0.0),
        risk_deny_events=int(raw.get("risk_deny_events", 0)),
        cycle_count=int(raw.get("cycle_count", 0)),
        avg_cycle_duration_ms=raw.get("avg_cycle_duration_ms"),
        market_source=str(raw.get("market_source") or market_source or "any"),
    )


@router.get("/stats/shadow", response_model=ShadowStatsResponse)
def get_shadow_stats(
    hours: int = Query(default=24, ge=1, le=168),
) -> ShadowStatsResponse:
    """Counts of shadow_fill events over the last ``hours`` (max 168)."""
    ledger = get_ledger()
    raw = ledger.get_shadow_stats(hours=float(hours))
    return ShadowStatsResponse(
        hours=hours,
        count=int(raw.get("count", 0)),
        by_action=dict(raw.get("by_action") or {}),
        by_policy=dict(raw.get("by_policy") or {}),
        probe_count=int(raw.get("probe_count", 0)),
        last_timestamp=raw.get("last_timestamp"),
    )


@router.get("/stats/quality", response_model=QualityStatsResponse)
def get_quality_stats(
    hours: int = Query(default=24, ge=1, le=168),
) -> QualityStatsResponse:
    """Compact observation quality scorecard over the last ``hours`` (max 168)."""
    ledger = get_ledger()
    raw = ledger.get_quality_stats(hours=float(hours))
    shadow_raw = raw.get("shadow") or {}
    return QualityStatsResponse(
        hours=hours,
        market_source=dict(raw.get("market_source") or {}),
        decision_count=int(raw.get("decision_count", 0)),
        wait_rate=float(raw.get("wait_rate") or 0.0),
        by_action=dict(raw.get("by_action") or {}),
        near_signal_rate=float(raw.get("near_signal_rate") or 0.0),
        shadow=QualityShadowBlock(
            count=int(shadow_raw.get("count", 0)),
            by_action=dict(shadow_raw.get("by_action") or {}),
            by_policy=dict(shadow_raw.get("by_policy") or {}),
            probe_count=int(shadow_raw.get("probe_count", 0)),
            last_timestamp=shadow_raw.get("last_timestamp"),
        ),
        cycle_count=int(raw.get("cycle_count", 0)),
        avg_cycle_duration_ms=raw.get("avg_cycle_duration_ms"),
    )
