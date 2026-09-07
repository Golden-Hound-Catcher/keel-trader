"""Q0 near-signal radar endpoints (read-only observation)."""
from __future__ import annotations

from fastapi import APIRouter, Query

from keel.api.deps import get_ledger
from keel.api.schemas import (
    NearestSignalItem,
    NearestSignalsResponse,
    NearestSignalsSummary,
)
from keel.config import get_settings

router = APIRouter()


@router.get("/signals/nearest", response_model=NearestSignalsResponse)
def get_nearest_signals(
    hours: int = Query(default=24, ge=1, le=168),
) -> NearestSignalsResponse:
    """
    Latest decision per configured instrument with ``signal_diag`` radar fields.

    Lookback ``hours`` (default 24, max 168). Summary counts: WAIT / near-long /
    near-short / fired long|short. Read-only observation UX — no trading.
    """
    settings = get_settings()
    instrument_ids = list(settings.instruments) if settings.instruments else None
    ledger = get_ledger()
    raw = ledger.get_nearest_signals(hours=float(hours), instrument_ids=instrument_ids)
    summary_raw = raw.get("summary") or {}
    signals = [
        NearestSignalItem(
            inst_id=str(s["inst_id"]),
            action=str(s["action"]),
            timestamp=float(s["timestamp"]),
            nearest=s.get("nearest"),
            missing=list(s.get("missing") or []),
            rsi_14=s.get("rsi_14"),
            trend_15m=s.get("trend_15m"),
            volume_ratio=s.get("volume_ratio"),
            ema_9=s.get("ema_9"),
            ema_21=s.get("ema_21"),
            macd_histogram=s.get("macd_histogram"),
        )
        for s in (raw.get("signals") or [])
    ]
    return NearestSignalsResponse(
        hours=hours,
        count=len(signals),
        summary=NearestSignalsSummary(
            waiting=int(summary_raw.get("waiting", 0)),
            long_nearest=int(summary_raw.get("long_nearest", 0)),
            short_nearest=int(summary_raw.get("short_nearest", 0)),
            fired_long=int(summary_raw.get("fired_long", 0)),
            fired_short=int(summary_raw.get("fired_short", 0)),
        ),
        signals=signals,
    )
