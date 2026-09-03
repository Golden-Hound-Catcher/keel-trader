"""Health check endpoints."""
from __future__ import annotations

import time

from fastapi import APIRouter

from keel import __version__
from keel.api.schemas import HealthResponse, ReadyResponse
from keel.config import get_settings

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
    """Readiness check."""
    settings = get_settings()
    return ReadyResponse(
        ready=True,
        okx_configured=settings.okx_configured,
        llm_configured=settings.llm_configured,
    )
