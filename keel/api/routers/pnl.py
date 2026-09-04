"""Daily PnL endpoints (ledger realized)."""
from __future__ import annotations

from datetime import datetime

from fastapi import APIRouter, HTTPException, Query

from keel.api.deps import get_ledger
from keel.api.schemas import DailyPnlResponse
from keel.domain.records import BJ_TZ

router = APIRouter()


@router.get("/pnl/daily", response_model=DailyPnlResponse)
def daily_pnl(
    date: str | None = Query(
        default=None,
        description="Beijing calendar date YYYY-MM-DD; default = today Beijing",
        pattern=r"^\d{4}-\d{2}-\d{2}$",
    ),
) -> DailyPnlResponse:
    """Realized PnL for a Beijing calendar day from the ledger trades table."""
    if date is None:
        date = datetime.now(BJ_TZ).strftime("%Y-%m-%d")
    else:
        try:
            datetime.strptime(date, "%Y-%m-%d")
        except ValueError as exc:
            raise HTTPException(status_code=422, detail="date must be YYYY-MM-DD") from exc

    realized = get_ledger().get_daily_pnl(date)
    return DailyPnlResponse(date=date, realized_pnl=realized, source="ledger")
