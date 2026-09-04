"""Shared helpers for worker cycle timestamps (status + ready)."""
from __future__ import annotations

import time
from datetime import datetime
from typing import Any

# Default stale threshold when interval is the classic 15min (900s):
# max(2 * 900, 900 + 300) = 1800. Prefer worker_stale_threshold_seconds().
WORKER_STALE_SECONDS = 1800


def worker_stale_threshold_seconds(cycle_interval_seconds: int) -> int:
    """Seconds after which last_cycle is considered stale for /ready (and status).

    Formula: ``max(interval * 2, interval + 300)`` so short intervals still get
    headroom (at least +5min) and longer intervals allow roughly one missed
    cycle before ready flips false.
    """
    interval = max(1, int(cycle_interval_seconds))
    return max(interval * 2, interval + 300)


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


def is_worker_stale(
    seconds: int | None,
    cycle_interval_seconds: int,
) -> bool:
    """True when lag is known and exceeds the interval-based stale threshold."""
    if seconds is None:
        return False
    return seconds > worker_stale_threshold_seconds(cycle_interval_seconds)
