"""
R9 offline near-entry markout — counterfactual WAIT near-signal outcomes.

For WAIT decisions with ``signal_diag.nearest`` in {long, short} and
1..max_missing gates missing, simulate a shadow entry at the decision
timestamp/price and compute markout (reuse ``shadow_markout`` /
``factor_snapshots``) net of the OKX fee model.

Read-only / recommend-only: never places orders, never mutates probe settings.
"""
from __future__ import annotations

import json
import statistics
from typing import Any, Sequence

from keel.exchange.okx_fees import (
    build_fee_model,
    crosses_standard_funding_boundary,
    fetch_public_funding_rate,
    funding_markout_bps,
)
from keel.execution.near_probe import probe_action_for_nearest
from keel.ledger.shadow_markout import (
    DEFAULT_MARKOUT_HORIZONS_SECONDS,
    lookup_later_price,
    markout_bps,
)

# Default: historical "near" = 1–2 missing gates (configurable via max_missing).
DEFAULT_MAX_MISSING = 2
DEFAULT_MIN_MISSING = 1
# Evidence hurdle for "would clear fees" fraction (taker RT ≈ 10 bps).
DEFAULT_CLEAR_HURDLE_BPS = 10.0
DEFAULT_CLEAR_HORIZON_SECONDS = 300

# Match export_decisions: cycle writes decision + factor with same timestamp.
_ENTRY_PRICE_MATCH_SECONDS = 0.05
_ENTRY_PRICE_FALLBACK_SECONDS = 30.0


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
    return sum(1 for v in values if v > 0) / len(values)


def _parse_json_obj(raw: Any) -> dict[str, Any]:
    if isinstance(raw, dict):
        return raw
    if not raw:
        return {}
    try:
        payload = json.loads(raw)
    except (TypeError, json.JSONDecodeError):
        return {}
    return payload if isinstance(payload, dict) else {}


def signal_diag_from_calculus(calculus: Any) -> dict[str, Any]:
    """Extract ``signal_diag`` dict from calculus_data (dict or JSON string)."""
    calc = _parse_json_obj(calculus)
    diag = calc.get("signal_diag")
    return diag if isinstance(diag, dict) else {}


def market_source_from_calculus(calculus: Any) -> str | None:
    calc = _parse_json_obj(calculus)
    raw = calc.get("market_source")
    if raw is None or raw == "":
        return None
    return str(raw)


def is_near_wait_diag(
    diag: dict[str, Any] | None,
    *,
    max_missing: int = DEFAULT_MAX_MISSING,
    min_missing: int = DEFAULT_MIN_MISSING,
) -> bool:
    """True when nearest ∈ {long,short} and missing count in [min, max]."""
    if not isinstance(diag, dict):
        return False
    nearest = diag.get("nearest")
    if nearest not in ("long", "short"):
        return False
    missing_raw = diag.get("missing")
    missing = missing_raw if isinstance(missing_raw, list) else []
    n = len(missing)
    lo = max(0, int(min_missing))
    hi = max(lo, int(max_missing))
    return lo <= n <= hi


def lookup_entry_price(
    conn: Any,
    *,
    inst_id: str,
    decision_ts: float,
    entry_price: float | None = None,
) -> tuple[float, str] | None:
    """
    Resolve shadow entry price for a WAIT decision.

    Preference: exact ``factor_snapshots`` match → nearby snapshot (±30s) →
    ``decisions.entry_price`` when positive.
    """
    if not inst_id:
        return None
    ts = float(decision_ts)
    row = conn.execute(
        "SELECT price FROM factor_snapshots "
        "WHERE inst_id = ? AND abs(timestamp - ?) < ? AND price > 0 "
        "ORDER BY abs(timestamp - ?) ASC LIMIT 1",
        (inst_id, ts, _ENTRY_PRICE_MATCH_SECONDS, ts),
    ).fetchone()
    if row is not None:
        return float(row["price"] if hasattr(row, "keys") else row[0]), "factor_snapshots"

    row = conn.execute(
        "SELECT price FROM factor_snapshots "
        "WHERE inst_id = ? AND abs(timestamp - ?) < ? AND price > 0 "
        "ORDER BY abs(timestamp - ?) ASC LIMIT 1",
        (inst_id, ts, _ENTRY_PRICE_FALLBACK_SECONDS, ts),
    ).fetchone()
    if row is not None:
        return float(row["price"] if hasattr(row, "keys") else row[0]), "factor_snapshots_near"

    if entry_price is not None:
        try:
            ep = float(entry_price)
        except (TypeError, ValueError):
            ep = 0.0
        if ep > 0:
            return ep, "decisions.entry_price"
    return None


def _horizon_stats(
    gross: Sequence[float],
    net_open: Sequence[float],
    net_rt: Sequence[float],
    *,
    horizon: int,
    skipped: int,
    clear_hurdle_bps: float,
) -> dict[str, Any]:
    clear_frac = None
    if net_rt:
        clear_frac = sum(1 for v in net_rt if v >= float(clear_hurdle_bps)) / len(net_rt)
    return {
        "horizon_seconds": int(horizon),
        "sample_count": len(gross),
        "skipped": int(skipped),
        "avg_markout_bps": _avg(gross),
        "median_markout_bps": _median(gross),
        "win_rate": _win_rate(gross),
        "avg_net_open_markout_bps": _avg(net_open),
        "median_net_open_markout_bps": _median(net_open),
        "win_rate_net_open": _win_rate(net_open),
        "avg_net_roundtrip_markout_bps": _avg(net_rt),
        "median_net_roundtrip_markout_bps": _median(net_rt),
        "win_rate_net_roundtrip": _win_rate(net_rt),
        "frac_clear_net_rt_hurdle": clear_frac,
        "clear_hurdle_bps": float(clear_hurdle_bps),
    }


def compute_near_entry_markout(
    conn: Any,
    *,
    hours: float = 168.0,
    horizons: Sequence[int] | None = None,
    max_missing: int = DEFAULT_MAX_MISSING,
    min_missing: int = DEFAULT_MIN_MISSING,
    market_source: str = "any",
    inst_id: str | None = None,
    inst_ids: Sequence[str] | None = None,
    now: float | None = None,
    settings: Any | None = None,
    fee_transport: Any | None = None,
    funding_transport: Any | None = None,
    apply_funding: bool = True,
    clear_hurdle_bps: float = DEFAULT_CLEAR_HURDLE_BPS,
    clear_horizon_seconds: int = DEFAULT_CLEAR_HORIZON_SECONDS,
    limit: int = 50_000,
) -> dict[str, Any]:
    """
    Aggregate counterfactual markout for WAIT near-signal decisions.

    Selects WAIT rows with nearest ∈ {long,short} and missing count in
    ``[min_missing, max_missing]``, simulates entry at decision price, then
    marks out vs later ``factor_snapshots`` (same helper as shadow fills).
    """
    import time as _time

    if settings is None:
        try:
            from keel.config import get_settings

            settings = get_settings()
        except Exception:
            settings = None

    hours_f = max(0.0, float(hours))
    now_ts = float(now if now is not None else _time.time())
    since = now_ts - hours_f * 3600.0
    if horizons is None:
        horizon_list = list(DEFAULT_MARKOUT_HORIZONS_SECONDS)
    else:
        horizon_list = sorted({int(h) for h in horizons if int(h) > 0})
    if not horizon_list:
        horizon_list = list(DEFAULT_MARKOUT_HORIZONS_SECONDS)

    fee_model = build_fee_model(settings, transport=fee_transport)
    open_fee_bps = float(fee_model["open_fee_bps"])
    rt_fee_bps = float(fee_model["round_trip_fee_bps"])

    ids: list[str] = []
    if inst_ids is not None:
        ids = [str(i).strip() for i in inst_ids if str(i).strip()]
    elif inst_id is not None and str(inst_id).strip():
        # Allow comma-separated --inst-id BTC,ETH
        parts = [p.strip() for p in str(inst_id).split(",") if p.strip()]
        ids = parts

    ms_raw = (market_source or "any").strip().lower()
    ms_filter = ms_raw if ms_raw in ("okx_public", "synthetic") else None

    query = "SELECT id, timestamp, inst_id, action, entry_price, calculus_data FROM decisions WHERE timestamp >= ?"
    params: list[Any] = [since]
    query += " AND UPPER(action) = 'WAIT'"
    if len(ids) == 1:
        query += " AND inst_id = ?"
        params.append(ids[0])
    elif len(ids) > 1:
        placeholders = ",".join("?" for _ in ids)
        query += f" AND inst_id IN ({placeholders})"
        params.extend(ids)
    if ms_filter:
        query += (
            " AND calculus_data IS NOT NULL"
            " AND json_extract(calculus_data, '$.market_source') = ?"
        )
        params.append(ms_filter)
    query += " ORDER BY timestamp ASC LIMIT ?"
    params.append(max(1, int(limit)))

    rows = conn.execute(query, params).fetchall()

    all_gross: dict[int, list[float]] = {h: [] for h in horizon_list}
    all_net_open: dict[int, list[float]] = {h: [] for h in horizon_list}
    all_net_rt: dict[int, list[float]] = {h: [] for h in horizon_list}
    skipped: dict[int, int] = {h: 0 for h in horizon_list}
    funding_applied_count: dict[int, int] = {h: 0 for h in horizon_list}
    by_action_gross: dict[int, dict[str, list[float]]] = {h: {} for h in horizon_list}
    by_action_net_rt: dict[int, dict[str, list[float]]] = {h: {} for h in horizon_list}

    by_nearest: dict[str, int] = {}
    by_missing_n: dict[str, int] = {}
    by_action_count: dict[str, int] = {}
    inst_count: dict[str, int] = {}
    inst_by_nearest: dict[str, dict[str, int]] = {}
    inst_gross_300: dict[str, list[float]] = {}
    inst_net_rt_300: dict[str, list[float]] = {}
    inst_by_missing: dict[str, dict[str, int]] = {}

    candidate_count = 0
    skipped_no_price = 0
    skipped_no_action = 0
    entry_sources: set[str] = set()
    later_sources: set[str] = set()
    last_ts: float | None = None

    funding_rates: dict[str, float | None] = {}
    funding_any_applied = False
    funding_any_crossed = False
    funding_fetch_failed = False

    clear_h = int(clear_horizon_seconds)
    clear_vals: list[float] = []

    for row in rows:
        ts = float(row["timestamp"])
        iid = str(row["inst_id"] or "").strip()
        calc_raw = row["calculus_data"]
        diag = signal_diag_from_calculus(calc_raw)
        if not is_near_wait_diag(
            diag, max_missing=max_missing, min_missing=min_missing
        ):
            continue

        nearest = str(diag.get("nearest"))
        missing = diag.get("missing") if isinstance(diag.get("missing"), list) else []
        n_missing = len(missing)
        action = probe_action_for_nearest(nearest)
        if action is None:
            skipped_no_action += 1
            continue

        ep_col = row["entry_price"]
        found_entry = lookup_entry_price(
            conn, inst_id=iid, decision_ts=ts, entry_price=ep_col
        )
        if found_entry is None:
            skipped_no_price += 1
            continue
        entry_px, entry_src = found_entry
        entry_sources.add(entry_src)

        candidate_count += 1
        by_nearest[nearest] = by_nearest.get(nearest, 0) + 1
        mk = str(n_missing)
        by_missing_n[mk] = by_missing_n.get(mk, 0) + 1
        by_action_count[action] = by_action_count.get(action, 0) + 1
        if last_ts is None or ts > last_ts:
            last_ts = ts

        inst_key = iid or "UNKNOWN"
        inst_count[inst_key] = inst_count.get(inst_key, 0) + 1
        ibn = inst_by_nearest.setdefault(inst_key, {})
        ibn[nearest] = ibn.get(nearest, 0) + 1
        ibm = inst_by_missing.setdefault(inst_key, {})
        ibm[mk] = ibm.get(mk, 0) + 1

        for h in horizon_list:
            if now_ts < ts + float(h):
                skipped[h] += 1
                continue
            found = lookup_later_price(
                conn,
                inst_id=iid,
                fill_ts=ts,
                horizon_seconds=float(h),
            )
            if found is None:
                skipped[h] += 1
                continue
            later_ts, later_price, source = found
            later_sources.add(source)
            gross = markout_bps(action, entry_px, later_price)
            if gross is None:
                skipped[h] += 1
                continue

            funding_adj = 0.0
            if apply_funding and crosses_standard_funding_boundary(ts, later_ts):
                funding_any_crossed = True
                if iid not in funding_rates:
                    funding_rates[iid] = fetch_public_funding_rate(
                        iid, transport=funding_transport
                    )
                    if funding_rates[iid] is None:
                        funding_fetch_failed = True
                rate = funding_rates.get(iid)
                if rate is not None:
                    funding_adj = funding_markout_bps(action, rate)
                    funding_applied_count[h] += 1
                    funding_any_applied = True

            net_open = gross - open_fee_bps + funding_adj
            net_rt = gross - rt_fee_bps + funding_adj

            all_gross[h].append(gross)
            all_net_open[h].append(net_open)
            all_net_rt[h].append(net_rt)
            by_action_gross[h].setdefault(action, []).append(gross)
            by_action_net_rt[h].setdefault(action, []).append(net_rt)

            if int(h) == 300:
                inst_gross_300.setdefault(inst_key, []).append(gross)
                inst_net_rt_300.setdefault(inst_key, []).append(net_rt)
            if int(h) == clear_h:
                clear_vals.append(net_rt)

    if funding_any_applied:
        fee_model["funding_note"] = (
            "Applied at most one standard UTC funding (00/08/16) when entry→horizon "
            "crossed a boundary and a public funding rate was available. "
            "Funding is separate from trading fees."
        )
    elif funding_any_crossed and funding_fetch_failed:
        fee_model["funding_note"] = (
            "Entry→horizon crossed a standard UTC funding boundary but public "
            "funding rate was unavailable; funding_applied=false (not invented). "
            "Funding is separate from trading fees."
        )
    else:
        fee_model["funding_note"] = (
            "No standard UTC funding boundary (00/08/16) crossed in sampled "
            "entry→horizon windows (short horizons usually 0); funding_applied=false. "
            "Funding is separate from trading fees."
        )
    fee_model["funding_applied"] = bool(funding_any_applied)

    horizons_out: list[dict[str, Any]] = []
    for h in horizon_list:
        stats = _horizon_stats(
            all_gross[h],
            all_net_open[h],
            all_net_rt[h],
            horizon=h,
            skipped=skipped[h],
            clear_hurdle_bps=clear_hurdle_bps,
        )
        action_stats: dict[str, dict[str, Any]] = {}
        for act, gvals in by_action_gross[h].items():
            nvals = by_action_net_rt[h].get(act, [])
            action_stats[act] = {
                "sample_count": len(gvals),
                "avg_markout_bps": _avg(gvals),
                "win_rate": _win_rate(gvals),
                "avg_net_roundtrip_markout_bps": _avg(nvals),
                "win_rate_net_roundtrip": _win_rate(nvals),
            }
        stats["by_action"] = action_stats
        stats["funding_applied_count"] = int(funding_applied_count[h])
        horizons_out.append(stats)

    frac_clear_300 = None
    if clear_vals:
        frac_clear_300 = sum(
            1 for v in clear_vals if v >= float(clear_hurdle_bps)
        ) / len(clear_vals)

    by_instrument: dict[str, dict[str, Any]] = {}
    for ik in sorted(inst_count.keys()):
        net_vals = inst_net_rt_300.get(ik) or []
        gross_vals = inst_gross_300.get(ik) or []
        entry: dict[str, Any] = {
            "count": int(inst_count.get(ik, 0)),
            "by_nearest": dict(inst_by_nearest.get(ik) or {}),
            "by_missing_n": dict(inst_by_missing.get(ik) or {}),
        }
        if 300 in horizon_list:
            clear_i = None
            if net_vals:
                clear_i = sum(
                    1 for v in net_vals if v >= float(clear_hurdle_bps)
                ) / len(net_vals)
            entry["markout_300s"] = {
                "sample_count": len(net_vals),
                "avg_markout_bps": _avg(gross_vals),
                "win_rate": _win_rate(gross_vals),
                "avg_net_roundtrip_markout_bps": _avg(net_vals),
                "win_rate_net_roundtrip": _win_rate(net_vals),
                "frac_clear_net_rt_hurdle": clear_i,
            }
        by_instrument[ik] = entry

    return {
        "hours": int(hours_f) if hours_f == int(hours_f) else hours_f,
        "count": candidate_count,
        "skipped_no_price": skipped_no_price,
        "skipped_no_action": skipped_no_action,
        "max_missing": int(max_missing),
        "min_missing": int(min_missing),
        "market_source": ms_filter or "any",
        "by_nearest": by_nearest,
        "by_missing_n": by_missing_n,
        "by_action": by_action_count,
        "by_instrument": by_instrument,
        "last_timestamp": last_ts,
        "fee_model": fee_model,
        "clear_hurdle_bps": float(clear_hurdle_bps),
        "clear_horizon_seconds": clear_h,
        "frac_clear_10bps_net_at_300s": frac_clear_300
        if clear_h == 300 and float(clear_hurdle_bps) == 10.0
        else frac_clear_300,
        "frac_clear_net_rt_hurdle": frac_clear_300,
        "entry_price_source": (
            ",".join(sorted(entry_sources)) if entry_sources else "factor_snapshots"
        ),
        "markout": {
            "price_source": (
                ",".join(sorted(later_sources)) if later_sources else "factor_snapshots"
            ),
            "horizons": horizons_out,
        },
        "recommend_only": True,
        "note": (
            "Counterfactual WAIT near-entry markout. Recommend-only — do not change "
            "probe settings automatically. Evidence for whether probes/near entries "
            "are worth enabling."
        ),
    }


__all__ = [
    "DEFAULT_MAX_MISSING",
    "DEFAULT_MIN_MISSING",
    "DEFAULT_CLEAR_HURDLE_BPS",
    "DEFAULT_CLEAR_HORIZON_SECONDS",
    "signal_diag_from_calculus",
    "market_source_from_calculus",
    "is_near_wait_diag",
    "lookup_entry_price",
    "compute_near_entry_markout",
]
