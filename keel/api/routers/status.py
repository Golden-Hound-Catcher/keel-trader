"""System status endpoints."""
from __future__ import annotations

import time

from fastapi import APIRouter

from keel import __version__
from keel.api.schemas import ConfigResponse, CredentialsStatus, StatusResponse
from keel.config import get_settings

router = APIRouter()

_START_TIME = time.time()


@router.get("/status", response_model=StatusResponse)
def status() -> StatusResponse:
    """Get system status."""
    settings = get_settings()
    return StatusResponse(
        version=__version__,
        mode="read_only_control_plane",
        uptime_seconds=int(time.time() - _START_TIME),
        environment=settings.okx_environment,
        credentials=CredentialsStatus(
            okx=settings.okx_configured,
            llm=settings.llm_configured,
        ),
        ledger_db=str(settings.ledger_path),
    )


@router.get("/config", response_model=ConfigResponse)
def config() -> ConfigResponse:
    """Get non-sensitive configuration."""
    settings = get_settings()
    return ConfigResponse(
        environment=settings.okx_environment,
        max_positions=settings.max_concurrent_positions,
        max_daily_loss=settings.max_daily_loss_usdt,
        max_asset_margin=settings.max_single_asset_margin,
        llm_model=settings.llm_model,
    )
