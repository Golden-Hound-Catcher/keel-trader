"""
Protect open LLM trades: widen fee-starved stops, then lift SL to BE+close-fee
after a real excursion — never a take-profit that only covers fees.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Any

from keel.config.settings import _env
from keel.policy.edge_overlay import plan_llm_geometry

logger = logging.getLogger("keel.execution.protect")

DEFAULT_BE_R = 0.5
DEFAULT_TAKER_BPS = 5.0


def _num(key: str, default: float) -> float:
    raw = (_env(key, "") or "").strip()
    if not raw:
        return default
    try:
        return float(raw)
    except ValueError:
        return default


def close_fee_pad(entry: float, *, taker_bps: float | None = None) -> float:
    bps = DEFAULT_TAKER_BPS if taker_bps is None else float(taker_bps)
    return float(entry) * max(0.0, bps) / 10_000.0


def favorable_r(side: str, entry: float, sl: float, mark: float) -> float:
    risk = abs(float(entry) - float(sl))
    if risk <= 0:
        return 0.0
    if str(side).lower() == "long":
        return (float(mark) - float(entry)) / risk
    return (float(entry) - float(mark)) / risk


@dataclass(frozen=True)
class ProtectPlan:
    side: str
    entry: float
    mark: float
    new_sl: float
    new_tp: float
    reason: str
    favorable_r: float
    widened: bool
    breakeven: bool


def plan_protect(
    *,
    side: str,
    entry: float,
    mark: float,
    sl: float | None,
    tp: float | None,
    atr: float,
    taker_bps: float = DEFAULT_TAKER_BPS,
    be_r: float | None = None,
) -> ProtectPlan | None:
    """
    Compute a wider SL / farther TP, and optionally a BE+close-fee stop.

    Fee-starved stops may widen (more room). Once a protective SL exists,
    ratchet only the favorable direction (long: max; short: min) so a later
    widen_fee_geometry pass cannot pull a locked BE back through entry.
    Never sets TP closer. BE only after ``be_r`` of risk is already in the
    money, so a scratch is insurance — not the profit plan.
    """
    side_s = str(side or "").strip().lower()
    if side_s not in ("long", "short"):
        return None
    entry_f = float(entry)
    mark_f = float(mark)
    if entry_f <= 0 or mark_f <= 0:
        return None
    geo = plan_llm_geometry(entry_f, max(float(atr or 0.0), 0.0))
    min_sl = float(geo["sl_dist"])
    min_tp = float(geo["tp_dist"])
    cur_sl = float(sl) if sl not in (None, 0) else (
        entry_f - min_sl if side_s == "long" else entry_f + min_sl
    )
    cur_tp = float(tp) if tp not in (None, 0) else (
        entry_f + min_tp if side_s == "long" else entry_f - min_tp
    )
    cur_sl_dist = abs(entry_f - cur_sl)
    cur_tp_dist = abs(cur_tp - entry_f)
    sl_dist = max(cur_sl_dist, min_sl)
    tp_dist = max(cur_tp_dist, min_tp, float(geo["tp_rr"]) * sl_dist)
    widened = sl_dist > cur_sl_dist + 1e-12 or tp_dist > cur_tp_dist + 1e-12
    # If SL is already at/above entry (long) or at/below entry (short), a
    # distance-from-entry recompute would mirror it back into loss territory
    # (9/23 BTC BE 86594 → 86031). Keep the locked stop as the baseline.
    already_be = (
        (side_s == "long" and cur_sl >= entry_f)
        or (side_s == "short" and cur_sl <= entry_f)
    )
    if already_be:
        new_sl = cur_sl
        # TP may still extend; risk for RR uses fee-floor geometry.
        risk = max(min_sl, 1e-12)
        tp_dist = max(cur_tp_dist, min_tp, float(geo["tp_rr"]) * risk)
        if side_s == "long":
            new_tp = entry_f + tp_dist
        else:
            new_tp = entry_f - tp_dist
        widened = tp_dist > cur_tp_dist + 1e-12
    elif side_s == "long":
        new_sl = entry_f - sl_dist
        new_tp = entry_f + tp_dist
    else:
        new_sl = entry_f + sl_dist
        new_tp = entry_f - tp_dist

    trigger = DEFAULT_BE_R if be_r is None else float(be_r)
    trigger = max(0.0, _num("KEEL_LLM_BE_R", trigger))
    # Use the stop we will actually have (widened) so BE does not fire on a
    # tiny leftover 1-ATR risk after we just opened the stop up.
    r_vs_new = favorable_r(side_s, entry_f, new_sl, mark_f)
    pad = close_fee_pad(entry_f, taker_bps=taker_bps)
    breakeven = False
    reason = "widen_fee_geometry" if widened else "hold"
    if trigger > 0 and r_vs_new >= trigger:
        if side_s == "long":
            be_sl = entry_f + pad
            cap = mark_f - pad
            if be_sl < cap and be_sl > new_sl:
                new_sl = be_sl
                breakeven = True
                reason = "breakeven_plus_close_fee"
        else:
            be_sl = entry_f - pad
            cap = mark_f + pad
            if be_sl > cap and be_sl < new_sl:
                new_sl = be_sl
                breakeven = True
                reason = "breakeven_plus_close_fee"

    # BE ratchet: once SL is at/above entry (long) or at/below (short), never
    # move it adversely (long: max; short: min). Risk-side fee-geometry widen
    # still allowed when the stop has not yet locked BE.
    if sl not in (None, 0):
        prior = float(sl)
        if side_s == "long" and prior >= entry_f:
            new_sl = max(new_sl, prior)
        elif side_s == "short" and prior <= entry_f:
            new_sl = min(new_sl, prior)

    if not widened and not breakeven and abs(new_sl - cur_sl) <= 1e-12 and abs(
        new_tp - cur_tp
    ) <= 1e-12:
        return None
    return ProtectPlan(
        side=side_s,
        entry=entry_f,
        mark=mark_f,
        new_sl=new_sl,
        new_tp=new_tp,
        reason=reason,
        favorable_r=r_vs_new,
        widened=widened,
        breakeven=breakeven,
    )


def _pos_field(pos: Any, name: str, default: Any = None) -> Any:
    if isinstance(pos, dict):
        return pos.get(name, default)
    return getattr(pos, name, default)


def protect_open_positions(
    exchange: Any,
    ledger: Any | None = None,
    *,
    now: float | None = None,
    atr_by_inst: dict[str, float] | None = None,
) -> list[ProtectPlan]:
    """Best-effort amend of live OCO TP/SL. No-op when the adapter cannot amend."""
    lister = getattr(exchange, "get_pending_oco", None)
    amender = getattr(exchange, "amend_oco_tpsl", None)
    if not callable(lister) or not callable(amender):
        return []
    getter = getattr(exchange, "get_positions", None)
    if not callable(getter):
        return []
    try:
        positions = list(getter() or [])
    except Exception:
        logger.debug("protect: get_positions failed", exc_info=True)
        return []
    try:
        algos = list(lister() or [])
    except Exception:
        logger.debug("protect: get_pending_oco failed", exc_info=True)
        return []

    applied: list[ProtectPlan] = []
    atr_map = atr_by_inst or {}
    for pos in positions:
        inst = str(_pos_field(pos, "inst_id") or "")
        side = str(_pos_field(pos, "side") or "").lower()
        entry = float(_pos_field(pos, "avg_price") or 0.0)
        mark = float(_pos_field(pos, "mark_price") or 0.0)
        if not inst or side not in ("long", "short") or entry <= 0 or mark <= 0:
            continue
        match = None
        for algo in algos:
            if str(algo.get("instId") or algo.get("inst_id") or "") != inst:
                continue
            pos_side = str(algo.get("posSide") or algo.get("pos_side") or "").lower()
            if pos_side and pos_side != side:
                continue
            match = algo
            break
        if match is None:
            continue
        sl_raw = match.get("slTriggerPx") or match.get("sl_trigger_price")
        tp_raw = match.get("tpTriggerPx") or match.get("tp_trigger_price")
        try:
            sl = float(sl_raw) if sl_raw not in (None, "") else None
        except (TypeError, ValueError):
            sl = None
        try:
            tp = float(tp_raw) if tp_raw not in (None, "") else None
        except (TypeError, ValueError):
            tp = None
        plan = plan_protect(
            side=side,
            entry=entry,
            mark=mark,
            sl=sl,
            tp=tp,
            atr=float(atr_map.get(inst) or 0.0),
        )
        if plan is None:
            continue
        algo_id = str(match.get("algoId") or match.get("algo_id") or "")
        if not algo_id:
            continue
        try:
            ok = bool(amender(inst, algo_id, sl=plan.new_sl, tp=plan.new_tp))
        except Exception:
            logger.warning("protect: amend failed %s", inst, exc_info=True)
            ok = False
        if not ok:
            continue
        applied.append(plan)
        if ledger is not None:
            try:
                ledger.record_event(
                    "sl_protect",
                    inst_id=inst,
                    data={
                        "reason": plan.reason,
                        "side": plan.side,
                        "entry": plan.entry,
                        "mark": plan.mark,
                        "new_sl": plan.new_sl,
                        "new_tp": plan.new_tp,
                        "favorable_r": plan.favorable_r,
                        "widened": plan.widened,
                        "breakeven": plan.breakeven,
                    },
                    timestamp=now,
                )
            except Exception:
                pass
    return applied


__all__ = [
    "ProtectPlan",
    "close_fee_pad",
    "favorable_r",
    "plan_protect",
    "protect_open_positions",
]
