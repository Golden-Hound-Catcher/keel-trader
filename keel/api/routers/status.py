"""System status endpoints."""
from __future__ import annotations

import time
from datetime import datetime
from typing import Any

from fastapi import APIRouter

from keel import __version__
from keel.api.deps import get_ledger
from keel.api.schemas import ConfigResponse, CredentialsStatus, LastCycleSummary, StatusResponse
from keel.config import get_settings
from keel.domain.instruments import InstrumentPool

router = APIRouter()

_START_TIME = time.time()


def _parse_cycle_timestamp(value: Any) -> float | None:
    """Parse last_cycle.timestamp (unix float/int or ISO-8601 string) → epoch seconds."""
    if value is None:
        return None
    if isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        ts = float(value)
        return ts if ts > 0 else None
    if isinstance(value, str):
        s = value.strip()
        if not s:
            return None
        try:
            ts = float(s)
            return ts if ts > 0 else None
        except ValueError:
            pass
        try:
            # Accept trailing Z
            iso = s.replace("Z", "+00:00")
            return datetime.fromisoformat(iso).timestamp()
        except ValueError:
            return None
    return None


def _seconds_since_last_cycle(last_raw: dict[str, Any] | None) -> int | None:
    if not last_raw:
        return None
    ts = _parse_cycle_timestamp(last_raw.get("timestamp"))
    if ts is None:
        return None
    return max(0, int(time.time() - ts))


@router.get("/status", response_model=StatusResponse)
def status() -> StatusResponse:
    """Get system status, including last worker cycle summary when available."""
    settings = get_settings()
    last_raw = get_ledger().get_last_cycle_summary()
    last_cycle = LastCycleSummary.model_validate(last_raw) if last_raw else None
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
        kill_switch=settings.kill_switch,
        last_cycle=last_cycle,
        seconds_since_last_cycle=_seconds_since_last_cycle(last_raw),
    )


@router.get("/config", response_model=ConfigResponse)
def config() -> ConfigResponse:
    """Get non-sensitive configuration."""
    settings = get_settings()
    instruments = [i.inst_id for i in InstrumentPool().all()]
    return ConfigResponse(
        environment=settings.okx_environment,
        max_positions=settings.max_concurrent_positions,
        max_daily_loss=settings.max_daily_loss_usdt,
        max_asset_margin=settings.max_single_asset_margin,
        llm_model=settings.llm_model,
        kill_switch=settings.kill_switch,
        instruments=instruments,
        notify_configured=settings.notify_configured,
        exchange_mode=settings.exchange_mode,
    )
