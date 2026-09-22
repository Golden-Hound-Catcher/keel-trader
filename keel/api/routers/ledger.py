"""Ledger operator endpoints (read-only inventory)."""
from __future__ import annotations

from fastapi import APIRouter, Query

from keel.api.deps import get_ledger
from keel.api.schemas import LedgerOrphanRow, LedgerOrphansResponse
from keel.ledger.orphan_inventory import inventory_orphans

router = APIRouter()


@router.get("/ledger/orphans", response_model=LedgerOrphansResponse)
def ledger_orphans(
    sample_limit: int = Query(20, ge=0, le=200),
) -> LedgerOrphansResponse:
    """List orphan opens (no linked close). Never writes closes."""
    inv = inventory_orphans(get_ledger(), sample_limit=sample_limit)
    detail = inv.to_detail_dict(include_sample=True)
    sample = [LedgerOrphanRow.model_validate(r) for r in detail.get("sample") or []]
    return LedgerOrphansResponse(
        total=inv.total,
        live=inv.live,
        shadow=inv.shadow,
        other=inv.other,
        by_inst=dict(inv.by_inst),
        by_strategy_tag=dict(inv.by_strategy_tag),
        oldest_age_seconds=inv.oldest_age_seconds,
        newest_age_seconds=inv.newest_age_seconds,
        sample=sample,
    )
