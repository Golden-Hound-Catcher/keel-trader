"""Health check endpoints."""
from __future__ import annotations

import time

from fastapi import APIRouter

from keel import __version__
from keel.api.cycle_time import is_worker_stale, seconds_since_last_cycle
from keel.api.deps import get_ledger
from keel.api.schemas import HealthResponse, ReadyResponse, ZeroEntryAlert
from keel.config import get_settings
from keel.ledger.zero_entry_alert import safe_compute_zero_entry_alert

router = APIRouter()


@router.get("/health", response_model=HealthResponse)
def health() -> HealthResponse:
    """Basic health check."""
    settings = get_settings()
    return HealthResponse(
        status="ok",
        service="keel-trader",
        version=__version__,
        timestamp=int(time.time()),
        environment="demo" if settings.is_demo else "live",
    )


@router.get("/ready", response_model=ReadyResponse)
def ready() -> ReadyResponse:
    """Readiness check with worker lag.

    ``ready`` is False when the ledger path is missing/unreadable, or when
    ``worker_stale`` is True (last cycle older than the interval-based
    threshold: max(2×cycle_interval, cycle_interval+300)). When there is no
    cycle yet (``seconds_since_last_cycle`` is null), cold start is OK:
    ``ready`` stays True if the ledger opens. ``okx_configured`` /
    ``llm_configured`` are always reported regardless of readiness.

    ``zero_entry_alert`` (48h no filled LLM entry; market vs outage) is
    informational and never changes ``ready``.
    """
    settings = get_settings()
    seconds: int | None = None
    ledger_ok = False
    zero_entry: dict | None = None
    try:
        ledger = get_ledger()
        # Touch the DB so missing/unreadable paths fail closed.
        last_raw = ledger.get_last_cycle_summary()
        seconds = seconds_since_last_cycle(last_raw)
        ledger_ok = True
        # 48h zero-entry alert: informational, never flips ``ready``.
        zero_entry = safe_compute_zero_entry_alert(
            ledger, cycle_interval_seconds=settings.cycle_interval_seconds
        )
    except Exception:
        ledger_ok = False

    worker_stale = is_worker_stale(seconds, settings.cycle_interval_seconds)
    return ReadyResponse(
        ready=bool(ledger_ok and not worker_stale),
        okx_configured=settings.okx_configured,
        llm_configured=settings.llm_configured,
        seconds_since_last_cycle=seconds,
        worker_stale=worker_stale,
        zero_entry_alert=ZeroEntryAlert.model_validate(zero_entry) if zero_entry else None,
    )
