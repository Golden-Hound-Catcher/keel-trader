"""
E1 full-gate fire detection + fee-aware markout.

A **full-gate fire** is a rule policy decision whose action is BUY_LONG or
SELL_SHORT and whose ``signal_diag.missing`` is empty (all entry gates passed).
Distinct from WAIT / near-signals (1–2 missing) and from forced paper fills
(no signal_diag).

Read-only / measurement-only: never places orders, never clears kill-switch,
never re-enables near_probe.
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
from keel.ledger.near_entry_markout import (
    DEFAULT_CLEAR_HURDLE_BPS,
    DEFAULT_CLEAR_HORIZON_SECONDS,
    lookup_entry_price,
    signal_diag_from_calculus,
)
from keel.ledger.shadow_markout import (
    DEFAULT_MARKOUT_HORIZONS_SECONDS,
    lookup_later_price,
    markout_bps,
)

FULL_GATE_ACTIONS = frozenset({"BUY_LONG", "SELL_SHORT"})
RULE_POLICY_NAMES = frozenset({"rule", ""})

# F1 cohort tags: post-E3.1 (strict TF with 4h) vs pre-E3.1 spray.
COHORT_POST_E31 = "post_e31"
COHORT_PRE_E31 = "pre_e31"
COHORT_STRICT_TF = "strict_tf"  # synonym for post_e31
COHORT_STALE_PRE_E31 = "stale_pre_e31"  # synonym for pre_e31
POST_E31_COHORTS = frozenset({COHORT_POST_E31, COHORT_STRICT_TF})
PRE_E31_COHORTS = frozenset({COHORT_PRE_E31, COHORT_STALE_PRE_E31})

# Match shadow_fill to a decision within this window (cycle cadence ~minutes).
_SHADOW_MATCH_SECONDS = 120.0


def _diag_truthy(value: Any) -> bool:
    """Truthy for require_4h_trend-style flags (bool/int/str)."""
    if value is True:
        return True
    if value is False or value is None:
        return False
    if isinstance(value, (int, float)):
        return value != 0
    s = str(value).strip().lower()
    return s in ("1", "true", "yes", "on")


def trend_gate_includes_4h(trend_gate: Any) -> bool:
    """True when ``trend_gate`` string contains the ``4h`` TF token (e.g. 15m+1h+4h)."""
    if trend_gate is None:
        return False
    return "4h" in str(trend_gate).strip().lower()


def is_post_e31_signal_diag(diag: dict[str, Any] | None) -> bool:
    """
    Post-E3.1 / strict_tf full-gate: ``require_4h_trend`` truthy OR ``trend_gate``
    contains ``4h``. Older TF fires without 4h require are pre_e31 / stale.
    """
    if not isinstance(diag, dict) or not diag:
        return False
    if _diag_truthy(diag.get("require_4h_trend")):
        return True
    return trend_gate_includes_4h(diag.get("trend_gate"))


def classify_full_gate_cohort(diag: dict[str, Any] | None) -> str:
    """
    Canonical cohort id for a full-gate fire: ``post_e31`` or ``pre_e31``.

    Synonyms (audit / Monitor): post_e31 ↔ strict_tf, pre_e31 ↔ stale_pre_e31.
    """
    if is_post_e31_signal_diag(diag):
        return COHORT_POST_E31
    return COHORT_PRE_E31


def normalize_cohort(cohort: str | None) -> str | None:
    """Map synonym → canonical; unknown/None → None."""
    if cohort is None:
        return None
    c = str(cohort).strip().lower()
    if not c or c in ("all", "full_gate", "any"):
        return None
    if c in POST_E31_COHORTS:
        return COHORT_POST_E31
    if c in PRE_E31_COHORTS:
        return COHORT_PRE_E31
    return c


def cohort_synonym(cohort: str | None) -> str:
    """Audit synonym for a canonical cohort."""
    c = normalize_cohort(cohort) or str(cohort or "").strip().lower()
    if c == COHORT_POST_E31:
        return COHORT_STRICT_TF
    if c == COHORT_PRE_E31:
        return COHORT_STALE_PRE_E31
    return c or COHORT_PRE_E31


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


def missing_list(diag: dict[str, Any] | None) -> list[str] | None:
    """
    Return missing-gate list when ``signal_diag`` is present.

    ``None`` means no usable signal_diag (not a full-gate candidate).
    Empty list means all gates passed.
    """
    if not isinstance(diag, dict) or not diag:
        return None
    # signal_diag must look like diagnose output (has nearest or missing key).
    if "missing" not in diag and "nearest" not in diag:
        return None
    raw = diag.get("missing")
    if raw is None:
        return []
    if isinstance(raw, list):
        return [str(x) for x in raw if x is not None and str(x)]
    return None


def is_full_gate_fire(
    action: Any,
    diag: dict[str, Any] | None,
    *,
    policy_name: str | None = None,
) -> bool:
    """
    True when action ∈ {BUY_LONG,SELL_SHORT}, diagnose missing==[], and policy
    is rule (or legacy empty policy_name).
    """
    act = str(action or "").upper().strip()
    if act not in FULL_GATE_ACTIONS:
        return False
    missing = missing_list(diag)
    if missing is None or len(missing) != 0:
        return False
    pol = str(policy_name or "").strip().lower()
    if pol and pol not in RULE_POLICY_NAMES:
        return False
    return True


def aggregate_full_gate_fires(
    conn: Any,
    *,
    since: float,
    limit: int = 100_000,
) -> dict[str, Any]:
    """Count full-gate fires in ``decisions`` since ``since`` (F1: +by_cohort)."""
    rows = conn.execute(
        "SELECT action, inst_id, policy_name, calculus_data FROM decisions "
        "WHERE timestamp >= ? AND UPPER(action) IN ('BUY_LONG', 'SELL_SHORT') "
        "ORDER BY timestamp ASC LIMIT ?",
        (float(since), max(1, int(limit))),
    ).fetchall()

    by_action: dict[str, int] = {}
    by_instrument: dict[str, int] = {}
    by_cohort: dict[str, dict[str, Any]] = {
        COHORT_POST_E31: {"count": 0, "by_action": {}, "by_instrument": {}},
        COHORT_PRE_E31: {"count": 0, "by_action": {}, "by_instrument": {}},
    }
    count = 0
    for row in rows:
        act = str(row["action"] or "")
        pol = str(row["policy_name"] or "") if "policy_name" in row.keys() else ""
        diag = signal_diag_from_calculus(row["calculus_data"])
        if not is_full_gate_fire(act, diag, policy_name=pol):
            continue
        count += 1
        by_action[act] = by_action.get(act, 0) + 1
        ik = str(row["inst_id"] or "").strip() or "UNKNOWN"
        by_instrument[ik] = by_instrument.get(ik, 0) + 1
        cohort = classify_full_gate_cohort(diag)
        bucket = by_cohort[cohort]
        bucket["count"] = int(bucket["count"]) + 1
        ba = bucket["by_action"]
        ba[act] = int(ba.get(act, 0)) + 1
        bi = bucket["by_instrument"]
        bi[ik] = int(bi.get(ik, 0)) + 1

    return {
        "count": count,
        "by_action": by_action,
        "by_instrument": by_instrument,
        "by_cohort": by_cohort,
    }


def _lookup_shadow_fill(
    conn: Any,
    *,
    inst_id: str,
    decision_ts: float,
) -> tuple[float, float, str] | None:
    """
    Prefer a non-probe shadow_fill near the decision timestamp.

    Returns ``(fill_ts, fill_price, source_tag)`` or None.
    """
    if not inst_id:
        return None
    ts = float(decision_ts)
    rows = conn.execute(
        "SELECT timestamp, data FROM events "
        "WHERE event_type = ? AND inst_id = ? "
        "AND abs(timestamp - ?) < ? "
        "ORDER BY abs(timestamp - ?) ASC LIMIT 8",
        ("shadow_fill", inst_id, ts, _SHADOW_MATCH_SECONDS, ts),
    ).fetchall()
    best_non_probe: tuple[float, float, str] | None = None
    best_any: tuple[float, float, str] | None = None
    for row in rows:
        payload = _parse_json_obj(row["data"])
        try:
            px = float(payload.get("price") or payload.get("fill_price") or 0)
        except (TypeError, ValueError):
            px = 0.0
        if px <= 0:
            continue
        fill_ts = float(row["timestamp"])
        is_probe = (
            payload.get("probe") is True
            or str(payload.get("policy") or "") == "shadow_near_probe"
        )
        tag = "shadow_fill_probe" if is_probe else "shadow_fill"
        cand = (fill_ts, px, tag)
        if best_any is None:
            best_any = cand
        if not is_probe and best_non_probe is None:
            best_non_probe = cand
    return best_non_probe or best_any


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


def compute_full_gate_markout(
    conn: Any,
    *,
    hours: float = 168.0,
    horizons: Sequence[int] | None = None,
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
    cohort: str | None = None,
) -> dict[str, Any]:
    """
    Fee-aware markout for full-gate fires (BUY_LONG/SELL_SHORT, missing==[]).

    Entry preference: matched non-probe ``shadow_fill`` → decision entry_price /
    factor_snapshots (same helpers as R9 near-entry). Horizons default 60/300/900.

    F1: ``cohort`` may be ``post_e31`` / ``strict_tf`` or ``pre_e31`` /
    ``stale_pre_e31`` to filter; ``None`` / ``all`` keeps every full-gate fire.
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
        ids = [p.strip() for p in str(inst_id).split(",") if p.strip()]

    ms_raw = (market_source or "any").strip().lower()
    ms_filter = ms_raw if ms_raw in ("okx_public", "synthetic") else None
    cohort_filter = normalize_cohort(cohort)

    query = (
        "SELECT id, timestamp, inst_id, action, entry_price, policy_name, calculus_data "
        "FROM decisions WHERE timestamp >= ? "
        "AND UPPER(action) IN ('BUY_LONG', 'SELL_SHORT')"
    )
    params: list[Any] = [since]
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

    by_action_count: dict[str, int] = {}
    inst_count: dict[str, int] = {}
    inst_by_action: dict[str, dict[str, int]] = {}
    inst_gross_300: dict[str, list[float]] = {}
    inst_net_rt_300: dict[str, list[float]] = {}
    entry_source_counts: dict[str, int] = {}

    candidate_count = 0
    skipped_no_price = 0
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
        act = str(row["action"] or "")
        pol = str(row["policy_name"] or "") if "policy_name" in row.keys() else ""
        diag = signal_diag_from_calculus(row["calculus_data"])
        if not is_full_gate_fire(act, diag, policy_name=pol):
            continue
        row_cohort = classify_full_gate_cohort(diag)
        if cohort_filter is not None and row_cohort != cohort_filter:
            continue

        action = act.upper().strip()
        entry_ts = ts
        entry_px: float | None = None
        entry_src = ""

        shadow = _lookup_shadow_fill(conn, inst_id=iid, decision_ts=ts)
        if shadow is not None:
            entry_ts, entry_px, entry_src = shadow
        else:
            ep_col = row["entry_price"]
            found_entry = lookup_entry_price(
                conn, inst_id=iid, decision_ts=ts, entry_price=ep_col
            )
            if found_entry is None:
                skipped_no_price += 1
                continue
            entry_px, entry_src = found_entry
            entry_src = f"counterfactual:{entry_src}"

        candidate_count += 1
        by_action_count[action] = by_action_count.get(action, 0) + 1
        entry_source_counts[entry_src] = entry_source_counts.get(entry_src, 0) + 1
        if last_ts is None or ts > last_ts:
            last_ts = ts

        inst_key = iid or "UNKNOWN"
        inst_count[inst_key] = inst_count.get(inst_key, 0) + 1
        iba = inst_by_action.setdefault(inst_key, {})
        iba[action] = iba.get(action, 0) + 1

        for h in horizon_list:
            if now_ts < entry_ts + float(h):
                skipped[h] += 1
                continue
            found = lookup_later_price(
                conn,
                inst_id=iid,
                fill_ts=entry_ts,
                horizon_seconds=float(h),
            )
            if found is None:
                skipped[h] += 1
                continue
            later_ts, later_price, _source = found
            gross = markout_bps(action, float(entry_px), later_price)
            if gross is None:
                skipped[h] += 1
                continue

            funding_adj = 0.0
            if apply_funding and crosses_standard_funding_boundary(entry_ts, later_ts):
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
            "crossed a boundary and a public funding rate was available."
        )
    elif funding_any_crossed and funding_fetch_failed:
        fee_model["funding_note"] = (
            "Entry→horizon crossed a funding boundary but public funding rate "
            "unavailable; funding_applied=false."
        )
    else:
        fee_model["funding_note"] = (
            "No standard UTC funding boundary crossed in sampled windows; "
            "funding_applied=false."
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

    frac_clear = None
    if clear_vals:
        frac_clear = sum(
            1 for v in clear_vals if v >= float(clear_hurdle_bps)
        ) / len(clear_vals)

    by_instrument: dict[str, dict[str, Any]] = {}
    for ik in sorted(inst_count.keys()):
        net_vals = inst_net_rt_300.get(ik) or []
        gross_vals = inst_gross_300.get(ik) or []
        entry: dict[str, Any] = {
            "count": int(inst_count.get(ik, 0)),
            "by_action": dict(inst_by_action.get(ik) or {}),
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
        "by_action": by_action_count,
        "by_instrument": by_instrument,
        "entry_sources": entry_source_counts,
        "last_timestamp": last_ts,
        "market_source": ms_raw,
        "fee_model": fee_model,
        "clear_hurdle_bps": float(clear_hurdle_bps),
        "clear_horizon_seconds": clear_h,
        "frac_clear_net_rt_hurdle": frac_clear,
        "markout": {
            "price_source": "shadow_fill_or_factor_snapshots",
            "horizons": horizons_out,
        },
        "cohort": cohort_filter or "full_gate",
        "cohort_synonym": (
            cohort_synonym(cohort_filter) if cohort_filter else "full_gate"
        ),
        "note": (
            "E1/F1 full-gate markout (recommend-only). Counts BUY_LONG/SELL_SHORT "
            "with signal_diag.missing==[] under rule policy. F1 cohort filter: "
            "post_e31 when require_4h_trend or trend_gate contains 4h; else "
            "pre_e31. Prefer shadow_fill entry when present; else counterfactual. "
            "Success later needs n≥20 and 5m netRT win≥0.55 on post_e31. "
            "Keep near_probe off (E0 freeze)."
        ),
    }


def summarize_full_gate_markout(result: dict[str, Any] | None) -> dict[str, Any]:
    """
    Compact quality-API / Monitor shape from ``compute_full_gate_markout``.

    Prefer horizon 300s fields at the top level; still include 60/900 when present.
    """
    if not isinstance(result, dict):
        return {
            "count": 0,
            "sample_count": 0,
            "horizon_seconds": DEFAULT_CLEAR_HORIZON_SECONDS,
            "win_rate_net_roundtrip": None,
            "avg_net_roundtrip_markout_bps": None,
            "frac_clear_net_rt_hurdle": None,
            "clear_hurdle_bps": DEFAULT_CLEAR_HURDLE_BPS,
            "horizons": [],
            "by_action": {},
            "cohort": "full_gate",
            "cohort_synonym": "full_gate",
        }
    horizons_in = []
    markout = result.get("markout") if isinstance(result.get("markout"), dict) else {}
    for row in markout.get("horizons") or []:
        if not isinstance(row, dict):
            continue
        try:
            h = int(row.get("horizon_seconds") or 0)
        except (TypeError, ValueError):
            continue
        if h <= 0:
            continue
        horizons_in.append(
            {
                "horizon_seconds": h,
                "sample_count": int(row.get("sample_count") or 0),
                "win_rate_net_roundtrip": row.get("win_rate_net_roundtrip"),
                "avg_net_roundtrip_markout_bps": row.get("avg_net_roundtrip_markout_bps"),
                "frac_clear_net_rt_hurdle": row.get("frac_clear_net_rt_hurdle"),
                "clear_hurdle_bps": row.get("clear_hurdle_bps", result.get("clear_hurdle_bps")),
            }
        )
    by_h = {int(r["horizon_seconds"]): r for r in horizons_in}
    primary = by_h.get(300) or (horizons_in[0] if horizons_in else {})
    cohort = str(result.get("cohort") or "full_gate")
    out = {
        "count": int(result.get("count") or 0),
        "sample_count": int(primary.get("sample_count") or 0),
        "horizon_seconds": int(primary.get("horizon_seconds") or DEFAULT_CLEAR_HORIZON_SECONDS),
        "win_rate_net_roundtrip": primary.get("win_rate_net_roundtrip"),
        "avg_net_roundtrip_markout_bps": primary.get("avg_net_roundtrip_markout_bps"),
        "frac_clear_net_rt_hurdle": primary.get("frac_clear_net_rt_hurdle"),
        "clear_hurdle_bps": float(
            primary.get("clear_hurdle_bps")
            if primary.get("clear_hurdle_bps") is not None
            else result.get("clear_hurdle_bps", DEFAULT_CLEAR_HURDLE_BPS)
        ),
        "horizons": horizons_in,
        "by_action": dict(result.get("by_action") or {}),
        "cohort": cohort,
        "cohort_synonym": str(
            result.get("cohort_synonym") or cohort_synonym(cohort)
        ),
    }
    if result.get("stale_pre_e31_note"):
        out["stale_pre_e31_note"] = result.get("stale_pre_e31_note")
    return out


def summarize_full_gate_markout_primary(
    post_raw: dict[str, Any] | None,
    pre_raw: dict[str, Any] | None = None,
) -> tuple[dict[str, Any], dict[str, Any]]:
    """
    F1: primary quality/Monitor summary prefers post_e31; expose pre separately.

    When post_e31 has no fires/samples, primary metrics stay empty/null (do not
    poison headline with pre_e31 ~24% spray). ``stale_pre_e31_note`` explains.
    """
    post_sum = summarize_full_gate_markout(post_raw)
    post_sum["cohort"] = COHORT_POST_E31
    post_sum["cohort_synonym"] = COHORT_STRICT_TF
    pre_sum = summarize_full_gate_markout(pre_raw)
    pre_sum["cohort"] = COHORT_PRE_E31
    pre_sum["cohort_synonym"] = COHORT_STALE_PRE_E31
    post_n = int(post_sum.get("count") or 0)
    post_sample = int(post_sum.get("sample_count") or 0)
    pre_n = int(pre_sum.get("count") or 0)
    if post_n == 0 and post_sample == 0 and pre_n > 0:
        post_sum["stale_pre_e31_note"] = (
            f"No post_e31/strict_tf full-gate samples; {pre_n} stale pre_e31 "
            "fires excluded from primary FG 5m metrics (see "
            "full_gate_markout_pre_e31)."
        )
        # Ensure headline win metrics stay null when empty post cohort.
        post_sum["win_rate_net_roundtrip"] = None
        post_sum["avg_net_roundtrip_markout_bps"] = None
        post_sum["frac_clear_net_rt_hurdle"] = None
    return post_sum, pre_sum
