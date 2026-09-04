"""Shared helpers for worker cycle timestamps (status + ready)."""
from __future__ import annotations

import time
from datetime import datetime
from typing import Any

# Worker considered stale when last cycle is older than this (seconds).
WORKER_STALE_SECONDS = 900


def parse_cycle_timestamp(value: Any) -> float | None:
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


def seconds_since_last_cycle(last_raw: dict[str, Any] | None) -> int | None:
    """Return integer lag since last_cycle.timestamp, or None if unknown / no cycle."""
    if not last_raw:
        return None
    ts = parse_cycle_timestamp(last_raw.get("timestamp"))
    if ts is None:
        return None
    return max(0, int(time.time() - ts))
