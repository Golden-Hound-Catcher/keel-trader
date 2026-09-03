"""Health check endpoints."""
from __future__ import annotations

import time
from fastapi import APIRouter

from keel import __version__
from keel.config import get_settings

router = APIRouter()


@router.get("/health")
def health():
    """Basic health check."""
    settings = get_settings()
    return {
        "status": "ok",
        "service": "keel-trader",
        "version": __version__,
        "timestamp": int(time.time()),
        "environment": "demo" if settings.is_demo else "live",
    }


@router.get("/ready")
def ready():
    """Readiness check."""
    settings = get_settings()
    return {
        "ready": True,
        "okx_configured": settings.okx_configured,
        "llm_configured": settings.llm_configured,
    }
