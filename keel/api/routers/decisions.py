"""Decision / trade / event history endpoints (SQLite ledger)."""
from __future__ import annotations

from fastapi import APIRouter, Query

from keel.api.deps import get_ledger
from keel.api.schemas import (
    DecisionItem,
    DecisionsResponse,
    EventItem,
    EventsResponse,
    LatestDecisionResponse,
    TradeItem,
    TradesResponse,
)

router = APIRouter()


def _decision_item(d) -> DecisionItem:
    return DecisionItem(
        id=d.id,
        timestamp=d.timestamp,
        inst_id=d.inst_id,
        action=d.action,
        confidence=d.confidence,
        entry_price=d.entry_price,
        take_profit=d.take_profit,
        stop_loss=d.stop_loss,
        reason=d.reason,
        calculus_data=d.calculus_data,
    )


@router.get("/decisions", response_model=DecisionsResponse)
def get_decisions(
    limit: int = Query(default=50, le=200),
    inst_id: str | None = None,
) -> DecisionsResponse:
    """Get recent AI / paper decisions from the ledger."""
    ledger = get_ledger()
    decisions = ledger.get_decisions(inst_id=inst_id, limit=limit)
    return DecisionsResponse(
        count=len(decisions),
        decisions=[_decision_item(d) for d in decisions],
    )


@router.get("/decisions/latest/{inst_id}", response_model=LatestDecisionResponse)
def get_latest_decision(
    inst_id: str, max_age: int = Query(default=300, le=3600)
) -> LatestDecisionResponse:
    """Get the latest decision for an instrument."""
    ledger = get_ledger()
    decision = ledger.get_latest_decision(inst_id, max_age_seconds=max_age)
    if not decision:
        return LatestDecisionResponse(found=False, inst_id=inst_id)
    return LatestDecisionResponse(
        found=True,
        inst_id=inst_id,
        decision=_decision_item(decision),
    )


@router.get("/trades", response_model=TradesResponse)
def get_trades(
    limit: int = Query(default=50, le=200),
    inst_id: str | None = None,
) -> TradesResponse:
    """Get recent trades recorded by the execution path."""
    ledger = get_ledger()
    trades = ledger.get_trades(inst_id=inst_id, limit=limit)
    return TradesResponse(
        count=len(trades),
        trades=[
            TradeItem(
                id=t.id,
                timestamp=t.timestamp,
                inst_id=t.inst_id,
                action=t.action,
                direction=t.direction,
                size=t.size,
                price=t.price,
                pnl=t.pnl,
                strategy_tag=t.strategy_tag,
                reason=t.reason,
                metadata=t.metadata,
            )
            for t in trades
        ],
    )


@router.get("/events", response_model=EventsResponse)
def get_events(
    limit: int = Query(default=50, le=200),
    event_type: str | None = None,
    inst_id: str | None = None,
) -> EventsResponse:
    """Get recent ledger events (risk blocks, fills, cycle completes)."""
    ledger = get_ledger()
    events = ledger.get_events(event_type=event_type, inst_id=inst_id, limit=limit)
    return EventsResponse(
        count=len(events),
        events=[
            EventItem(
                id=e.id,
                timestamp=e.timestamp,
                event_type=e.event_type,
                inst_id=e.inst_id,
                data=e.data,
            )
            for e in events
        ],
    )
