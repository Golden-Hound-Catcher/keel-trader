"""Decision history endpoints."""
from __future__ import annotations

import time
from fastapi import APIRouter, Query

from keel.ledger import KeelLedger

router = APIRouter()


def _get_ledger() -> KeelLedger:
    """Get ledger instance."""
    return KeelLedger()


@router.get("/decisions")
def get_decisions(
    limit: int = Query(default=50, le=200),
    inst_id: str | None = None,
):
    """Get recent AI decisions."""
    ledger = _get_ledger()
    decisions = ledger.get_decisions(inst_id=inst_id, limit=limit)
    return {
        "count": len(decisions),
        "decisions": [
            {
                "id": d.id,
                "timestamp": d.timestamp,
                "inst_id": d.inst_id,
                "action": d.action,
                "confidence": d.confidence,
                "entry_price": d.entry_price,
                "take_profit": d.take_profit,
                "stop_loss": d.stop_loss,
                "reason": d.reason,
            }
            for d in decisions
        ],
    }


@router.get("/decisions/latest/{inst_id}")
def get_latest_decision(inst_id: str, max_age: int = Query(default=300, le=3600)):
    """Get the latest decision for an instrument."""
    ledger = _get_ledger()
    decision = ledger.get_latest_decision(inst_id, max_age_seconds=max_age)
    if not decision:
        return {"found": False, "inst_id": inst_id}
    return {
        "found": True,
        "decision": {
            "id": decision.id,
            "timestamp": decision.timestamp,
            "inst_id": decision.inst_id,
            "action": decision.action,
            "confidence": decision.confidence,
            "entry_price": decision.entry_price,
            "take_profit": decision.take_profit,
            "stop_loss": decision.stop_loss,
            "reason": decision.reason,
        },
    }
