"""Daily PnL endpoints (ledger realized + exchange float)."""
from __future__ import annotations

from datetime import datetime

from fastapi import APIRouter, HTTPException, Query

from keel.api.deps import get_ledger
from keel.api.routers.positions import _get_exchange
from keel.api.schemas import DailyPnlResponse
from keel.domain.records import BJ_TZ
from keel.exchange import OKXRestAdapter

router = APIRouter()


@router.get("/pnl/daily", response_model=DailyPnlResponse)
def daily_pnl(
    date: str | None = Query(
        default=None,
        description="Beijing calendar date YYYY-MM-DD; default = today Beijing",
        pattern=r"^\d{4}-\d{2}-\d{2}$",
    ),
) -> DailyPnlResponse:
    """Beijing-day PnL: ledger realized plus current unrealized (soft-fail)."""
    if date is None:
        date = datetime.now(BJ_TZ).strftime("%Y-%m-%d")
    else:
        try:
            datetime.strptime(date, "%Y-%m-%d")
        except ValueError as exc:
            raise HTTPException(status_code=422, detail="date must be YYYY-MM-DD") from exc

    realized = float(get_ledger().get_daily_pnl(date))
    unrealized: float | None = None
    unrealized_source: str | None = None
    try:
        exchange = _get_exchange()
        unrealized = float(exchange.get_balance().unrealized_pnl)
        unrealized_source = "okx" if isinstance(exchange, OKXRestAdapter) else "paper"
    except Exception:
        unrealized = None
        unrealized_source = None
    total = realized + unrealized if unrealized is not None else realized
    return DailyPnlResponse(
        date=date,
        realized_pnl=realized,
        unrealized_pnl=unrealized,
        total_pnl=total,
        source="ledger",
        unrealized_source=unrealized_source,
    )
