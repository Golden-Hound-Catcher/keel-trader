"""
Q3.2 shadow fill markout — offline outcome stats from ledger data.

For each ``shadow_fill`` event, compare fill price to a later mid/last from
``factor_snapshots`` (preferred) or subsequent ``decisions.entry_price``.
Horizons are pragmatic wall-clock offsets (e.g. 60s / 300s / 900s ≈ 1 cycle).
Read-only; never places orders or clears kill-switch.
"""
from __future__ import annotations

import json
import statistics
from typing import Any, Sequence

# Default horizons: 1m, 5m, ~1 default cycle (15m).
DEFAULT_MARKOUT_HORIZONS_SECONDS: tuple[int, ...] = (60, 300, 900)

# Accept first later price in [fill+h, fill+h+slack].
# Slack is generous so short horizons still resolve when cycle cadence is ~5m.
_MIN_SLACK_SECONDS = 600.0


def markout_bps(action: str, fill_price: float, later_price: float) -> float | None:
    """
    Directional markout in basis points.

    BUY_LONG / long:  (later - fill) / fill * 1e4
    SELL_SHORT / short: (fill - later) / fill * 1e4
    """
    if fill_price is None or later_price is None:
        return None
    try:
        fp = float(fill_price)
        lp = float(later_price)
    except (TypeError, ValueError):
        return None
    if fp <= 0 or lp <= 0:
        return None
    act = str(action or "").upper().strip()
    if act in ("BUY_LONG", "LONG", "BUY"):
        return (lp - fp) / fp * 10_000.0
    if act in ("SELL_SHORT", "SHORT", "SELL"):
        return (fp - lp) / fp * 10_000.0
    # Unknown action — treat as long for pragmatism only if clearly directional.
    return None


def _median(values: Sequence[float]) -> float | None:
    if not values:
        return None
    return float(statistics.median(values))


def _avg(values: Sequence[float]) -> float | None:
    if not values:
        return None
    return float(sum(values) / len(values))


def _win_rate(values: Sequence[float]) -> float | None:
    if not values:
        return None
    wins = sum(1 for v in values if v > 0)
    return wins / len(values)


def _parse_fill_payload(raw: Any) -> dict[str, Any]:
    if isinstance(raw, dict):
        return raw
    if not raw:
        return {}
    try:
        payload = json.loads(raw)
    except (TypeError, json.JSONDecodeError):
        return {}
    return payload if isinstance(payload, dict) else {}


def _classify_policy(payload: dict[str, Any]) -> str:
    if payload.get("policy"):
        return str(payload["policy"])
    if payload.get("probe") is True:
        return "shadow_near_probe"
    return "manual"


def _is_probe(payload: dict[str, Any], policy: str) -> bool:
    return policy == "shadow_near_probe" or payload.get("probe") is True


def lookup_later_price(
    conn: Any,
    *,
    inst_id: str,
    fill_ts: float,
    horizon_seconds: float,
) -> tuple[float, float, str] | None:
    """
    Find first usable later price after ``fill_ts + horizon``.

    Prefers ``factor_snapshots.price``, then ``decisions.entry_price``.
    Returns ``(later_ts, later_price, source)`` or None when unavailable /
    outside slack window (slack = max(2×horizon, 600s)).
    """
    h = max(0.0, float(horizon_seconds))
    target = float(fill_ts) + h
    slack = max(h * 2.0, _MIN_SLACK_SECONDS)
    deadline = target + slack

    row = conn.execute(
        "SELECT timestamp, price FROM factor_snapshots "
        "WHERE inst_id = ? AND timestamp >= ? AND timestamp <= ? AND price > 0 "
        "ORDER BY timestamp ASC LIMIT 1",
        (inst_id, target, deadline),
    ).fetchone()
    if row is not None:
        return float(row["timestamp"]), float(row["price"]), "factor_snapshots"

    # Fallback: decision entry_price in the same window (often NULL — skip ok).
    row = conn.execute(
        "SELECT timestamp, entry_price FROM decisions "
        "WHERE inst_id = ? AND timestamp >= ? AND timestamp <= ? "
        "AND entry_price IS NOT NULL AND entry_price > 0 "
        "ORDER BY timestamp ASC LIMIT 1",
        (inst_id, target, deadline),
    ).fetchone()
    if row is not None:
        return float(row["timestamp"]), float(row["entry_price"]), "decisions.entry_price"

    return None


def _empty_horizon(horizon: int) -> dict[str, Any]:
    return {
        "horizon_seconds": int(horizon),
        "sample_count": 0,
        "skipped": 0,
        "avg_markout_bps": None,
        "median_markout_bps": None,
        "win_rate": None,
        "probe_sample_count": 0,
        "probe_avg_markout_bps": None,
        "probe_median_markout_bps": None,
        "probe_win_rate": None,
        "by_action": {},
    }


def compute_shadow_markout(
    conn: Any,
    *,
    hours: float = 24.0,
    horizons: Sequence[int] | None = None,
    now: float | None = None,
) -> dict[str, Any]:
    """
    Aggregate markout stats for shadow_fill events in the lookback window.

    Offline-safe: reads only ``events`` + ``factor_snapshots`` / ``decisions``.
    Fills without a later price are counted in ``skipped`` per horizon.
    """
    import time as _time

    hours_f = max(0.0, float(hours))
    now_ts = float(now if now is not None else _time.time())
    since = now_ts - hours_f * 3600.0
    if horizons is None:
        horizon_list = list(DEFAULT_MARKOUT_HORIZONS_SECONDS)
    else:
        horizon_list = sorted({int(h) for h in horizons if int(h) > 0})
    if not horizon_list:
        horizon_list = list(DEFAULT_MARKOUT_HORIZONS_SECONDS)

    rows = conn.execute(
        "SELECT timestamp, inst_id, data FROM events "
        "WHERE timestamp >= ? AND event_type = ? "
        "ORDER BY timestamp ASC",
        (since, "shadow_fill"),
    ).fetchall()

    # Per-horizon accumulators.
    all_bps: dict[int, list[float]] = {h: [] for h in horizon_list}
    probe_bps: dict[int, list[float]] = {h: [] for h in horizon_list}
    skipped: dict[int, int] = {h: 0 for h in horizon_list}
    by_action_bps: dict[int, dict[str, list[float]]] = {h: {} for h in horizon_list}
    sources_used: set[str] = set()

    fill_count = 0
    probe_count = 0
    by_action: dict[str, int] = {}
    by_policy: dict[str, int] = {}
    last_ts: float | None = None

    for row in rows:
        fill_ts = float(row["timestamp"])
        inst_id = str(row["inst_id"] or "")
        payload = _parse_fill_payload(row["data"])
        action = str(payload.get("action") or "UNKNOWN")
        policy = _classify_policy(payload)
        is_probe = _is_probe(payload, policy)
        fill_price = payload.get("price")
        try:
            fill_price_f = float(fill_price) if fill_price is not None else 0.0
        except (TypeError, ValueError):
            fill_price_f = 0.0

        fill_count += 1
        if is_probe:
            probe_count += 1
        by_action[action] = by_action.get(action, 0) + 1
        by_policy[policy] = by_policy.get(policy, 0) + 1
        if last_ts is None or fill_ts > last_ts:
            last_ts = fill_ts

        for h in horizon_list:
            if fill_price_f <= 0 or not inst_id:
                skipped[h] += 1
                continue
            # Need enough wall time after fill for the horizon to be observable.
            if now_ts < fill_ts + float(h):
                skipped[h] += 1
                continue
            found = lookup_later_price(
                conn,
                inst_id=inst_id,
                fill_ts=fill_ts,
                horizon_seconds=float(h),
            )
            if found is None:
                skipped[h] += 1
                continue
            later_ts, later_price, source = found
            sources_used.add(source)
            bps = markout_bps(action, fill_price_f, later_price)
            if bps is None:
                skipped[h] += 1
                continue
            all_bps[h].append(bps)
            if is_probe:
                probe_bps[h].append(bps)
            by_action_bps[h].setdefault(action, []).append(bps)
            _ = later_ts  # available for future per-fill debug; aggregate only now

    horizons_out: list[dict[str, Any]] = []
    for h in horizon_list:
        vals = all_bps[h]
        pvals = probe_bps[h]
        action_stats: dict[str, dict[str, Any]] = {}
        for act, act_vals in by_action_bps[h].items():
            action_stats[act] = {
                "sample_count": len(act_vals),
                "avg_markout_bps": _avg(act_vals),
                "median_markout_bps": _median(act_vals),
                "win_rate": _win_rate(act_vals),
            }
        horizons_out.append(
            {
                "horizon_seconds": int(h),
                "sample_count": len(vals),
                "skipped": int(skipped[h]),
                "avg_markout_bps": _avg(vals),
                "median_markout_bps": _median(vals),
                "win_rate": _win_rate(vals),
                "probe_sample_count": len(pvals),
                "probe_avg_markout_bps": _avg(pvals),
                "probe_median_markout_bps": _median(pvals),
                "probe_win_rate": _win_rate(pvals),
                "by_action": action_stats,
            }
        )

    return {
        "hours": int(hours_f) if hours_f == int(hours_f) else hours_f,
        "count": fill_count,
        "probe_count": probe_count,
        "by_action": by_action,
        "by_policy": by_policy,
        "last_timestamp": last_ts,
        "markout": {
            "price_source": (
                ",".join(sorted(sources_used)) if sources_used else "factor_snapshots"
            ),
            "horizons": horizons_out,
        },
    }


__all__ = [
    "DEFAULT_MARKOUT_HORIZONS_SECONDS",
    "markout_bps",
    "lookup_later_price",
    "compute_shadow_markout",
]
