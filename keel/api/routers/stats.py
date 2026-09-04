"""Read-only decision / cycle observability stats."""
from __future__ import annotations

from fastapi import APIRouter, Query

from keel.api.deps import get_ledger
from keel.api.schemas import DecisionStatsResponse

router = APIRouter()


@router.get("/stats/decisions", response_model=DecisionStatsResponse)
def get_decision_stats(
    hours: int = Query(default=24, ge=1, le=168),
) -> DecisionStatsResponse:
    """Aggregate decision quality stats over the last ``hours`` (max 168)."""
    ledger = get_ledger()
    raw = ledger.get_decision_stats(hours=float(hours))
    return DecisionStatsResponse(
        hours=hours,
        decision_count=int(raw.get("decision_count", 0)),
        by_action=dict(raw.get("by_action") or {}),
        by_policy=dict(raw.get("by_policy") or {}),
        wait_rate=float(raw.get("wait_rate") or 0.0),
        risk_deny_events=int(raw.get("risk_deny_events", 0)),
        cycle_count=int(raw.get("cycle_count", 0)),
        avg_cycle_duration_ms=raw.get("avg_cycle_duration_ms"),
    )
