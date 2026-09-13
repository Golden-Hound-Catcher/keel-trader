"""
Hard edge overlay for ``KEEL_DECISION_POLICY=llm``.

Kernel (not the model):
- WAIT unless 1h (and by default 4h) agree with the side
- RSI chase veto (long>70 / short<30)
- Fee-aware TP/SL: stop must be several times round-trip fees; take-profit
  must be a multiple of that — never a "cover the fee" scalp
- Optional book lock: no scale-in, no hedge

Does not apply to ``rule`` / ``llm_veto`` (those already have variant gates).
"""
from __future__ import annotations

from typing import Any, Iterable

from keel.config.settings import _env
from keel.domain.decision import Decision, validate_decision
from keel.factors.market_data import MarketSnapshot

_FIRE_ACTIONS = frozenset({"BUY_LONG", "SELL_SHORT"})
_RSI_CHASE_LONG_MAX = 70.0
_RSI_CHASE_SHORT_MIN = 30.0
HTF_GATE = "htf_ok"
RSI_CHASE_GATE = "rsi_chase_ok"
GEOMETRY_GATE = "geometry_ok"
BOOK_GATE = "book_ok"

DEFAULT_SL_ATR = 2.0
DEFAULT_TP_RR = 2.2
DEFAULT_RT_FEE_BPS = 10.0
DEFAULT_FEE_SL_MULT = 6.0
DEFAULT_FEE_TP_MULT = 12.0


def _flag(key: str, default: bool) -> bool:
    raw = (_env(key, "") or "").strip().lower()
    if not raw:
        return default
    if raw in ("1", "true", "yes", "on"):
        return True
    if raw in ("0", "false", "no", "off"):
        return False
    return default


def _num(key: str, default: float) -> float:
    raw = (_env(key, "") or "").strip()
    if not raw:
        return default
    try:
        return float(raw)
    except ValueError:
        return default


def llm_edge_overlay_enabled() -> bool:
    """Default on. Set ``KEEL_LLM_EDGE_OVERLAY=0`` to A/B the raw model."""
    return _flag("KEEL_LLM_EDGE_OVERLAY", True)


def llm_book_lock_enabled() -> bool:
    """Default on. Blocks scale-in and opposite-side hedge."""
    return _flag("KEEL_LLM_NO_SCALE_IN", True)


def _require_1h() -> bool:
    return _flag("KEEL_LLM_REQUIRE_1H", True)


def _require_4h() -> bool:
    return _flag("KEEL_LLM_REQUIRE_4H", True)


def _trend(snapshot: MarketSnapshot, attr: str) -> str:
    return str(getattr(snapshot, attr, "neutral") or "neutral").strip().lower()


def _wait(decision: Decision, reason: str, *, gate: str, extra: dict[str, Any] | None = None) -> Decision:
    diag = dict(decision.signal_diag or {})
    prev = str(diag.get("nearest") or "").strip().lower()
    if prev in ("long", "short"):
        diag.setdefault("nearest_before_suppress", prev)
    diag["nearest"] = "none"
    missing_raw = diag.get("missing")
    missing = [str(x) for x in missing_raw] if isinstance(missing_raw, list) else []
    if gate and gate not in missing:
        missing.append(gate)
    diag["missing"] = missing
    diag[gate] = False
    if extra:
        diag.update(extra)
    return Decision(
        inst_id=decision.inst_id,
        action="WAIT",
        confidence=min(float(decision.confidence or 0.0), 40.0),
        reason=f"{decision.reason} | {reason}".strip(" |"),
        signal_diag=diag,
    )


def _htf_ok(snapshot: MarketSnapshot, side: str) -> tuple[bool, dict[str, Any]]:
    t1h = _trend(snapshot, "trend_1h")
    t4h = _trend(snapshot, "trend_4h")
    need_1h = _require_1h()
    need_4h = _require_4h()
    if side == "long":
        ok_1h = (not need_1h) or t1h == "bullish"
        ok_4h = (not need_4h) or t4h == "bullish"
    else:
        ok_1h = (not need_1h) or t1h == "bearish"
        ok_4h = (not need_4h) or t4h == "bearish"
    audit = {
        "trend_1h": t1h,
        "trend_4h": t4h,
        "require_1h_trend": need_1h,
        "require_4h_trend": need_4h,
        "htf_ok": bool(ok_1h and ok_4h),
    }
    return bool(ok_1h and ok_4h), audit


def _rsi_chase_ok(snapshot: MarketSnapshot, side: str) -> bool:
    rsi = float(getattr(snapshot, "rsi_14", 50.0) or 50.0)
    if side == "long":
        return rsi <= _RSI_CHASE_LONG_MAX
    return rsi >= _RSI_CHASE_SHORT_MIN


def plan_llm_geometry(entry: float, atr: float) -> dict[str, float]:
    """
    Stop / target distances in price units.

    ``sl_dist = max(sl_atr * ATR, entry * rt_fee_bps/1e4 * fee_sl_mult)``
    ``tp_dist = max(tp_rr * sl_dist, entry * rt_fee_bps/1e4 * fee_tp_mult)``

    Conservative RT = taker+taker (SL is a taker). Default 10 bps × 6 / 12
    so 1R is not fee-sized and a full win is not a fee-recovery scalp.
    """
    sl_atr = max(0.0, _num("KEEL_LLM_SL_ATR", DEFAULT_SL_ATR))
    tp_rr = max(2.0, _num("KEEL_LLM_TP_RR", DEFAULT_TP_RR))
    rt_bps = max(0.0, _num("KEEL_LLM_RT_FEE_BPS", DEFAULT_RT_FEE_BPS))
    sl_mult = max(1.0, _num("KEEL_LLM_FEE_SL_MULT", DEFAULT_FEE_SL_MULT))
    tp_mult = max(sl_mult, _num("KEEL_LLM_FEE_TP_MULT", DEFAULT_FEE_TP_MULT))
    fee_unit = float(entry) * (rt_bps / 10_000.0)
    fee_sl = fee_unit * sl_mult
    fee_tp = fee_unit * tp_mult
    sl_dist = max(sl_atr * float(atr), fee_sl)
    tp_dist = max(tp_rr * sl_dist, fee_tp)
    return {
        "sl_dist": sl_dist,
        "tp_dist": tp_dist,
        "sl_atr": sl_atr,
        "tp_rr": tp_rr,
        "rt_fee_bps": rt_bps,
        "fee_sl_mult": sl_mult,
        "fee_tp_mult": tp_mult,
        "fee_sl_px": fee_sl,
        "fee_tp_px": fee_tp,
        "atr_14": float(atr),
    }


def _rewrite_geometry(decision: Decision, snapshot: MarketSnapshot) -> Decision:
    price = float(snapshot.price or 0.0)
    atr = float(snapshot.atr_14 or 0.0)
    if price <= 0.0 or atr <= 0.0:
        return _wait(
            decision,
            "invalid atr/price for fee-aware geometry",
            gate=GEOMETRY_GATE,
        )
    entry = float(decision.entry_price or 0.0)
    if entry <= 0.0:
        entry = price
    plan = plan_llm_geometry(entry, atr)
    sl_dist = float(plan["sl_dist"])
    tp_dist = float(plan["tp_dist"])
    if decision.action == "BUY_LONG":
        sl = entry - sl_dist
        tp = entry + tp_dist
    else:
        sl = entry + sl_dist
        tp = entry - tp_dist
    diag = dict(decision.signal_diag or {})
    diag["geometry_ok"] = True
    diag.update(plan)
    out = Decision(
        inst_id=decision.inst_id,
        action=decision.action,
        confidence=decision.confidence,
        entry_price=entry,
        take_profit=tp,
        stop_loss=sl,
        leverage=decision.leverage,
        margin_usdt=decision.margin_usdt,
        reason=(
            f"{decision.reason} | geometry sl={sl_dist:.6g} tp={tp_dist:.6g} "
            f"(atr×{plan['sl_atr']:g} / rr {plan['tp_rr']:g}, "
            f"fee×{plan['fee_sl_mult']:g}/{plan['fee_tp_mult']:g})"
        ).strip(),
        signal_diag=diag,
    )
    return validate_decision(out)


def apply_llm_edge_overlay(decision: Decision, snapshot: MarketSnapshot) -> Decision:
    """
    Gate + rewrite one LLM fire. WAIT decisions pass through.

    Disable with ``KEEL_LLM_EDGE_OVERLAY=0``.
    """
    if not llm_edge_overlay_enabled():
        return decision
    if decision.action not in _FIRE_ACTIONS:
        return decision

    side = "long" if decision.action == "BUY_LONG" else "short"
    htf_ok, htf_audit = _htf_ok(snapshot, side)
    if not htf_ok:
        return _wait(
            decision,
            f"htf {side} needs 1h/4h agreement (t1h={htf_audit['trend_1h']} t4h={htf_audit['trend_4h']})",
            gate=HTF_GATE,
            extra=htf_audit,
        )
    if not _rsi_chase_ok(snapshot, side):
        rsi = float(getattr(snapshot, "rsi_14", 0.0) or 0.0)
        return _wait(
            decision,
            f"rsi chase veto rsi={rsi:.1f}",
            gate=RSI_CHASE_GATE,
            extra={**htf_audit, "rsi_14": rsi, HTF_GATE: True},
        )
    out = _rewrite_geometry(decision, snapshot)
    if out.signal_diag is None:
        out.signal_diag = {}
    out.signal_diag.update(htf_audit)
    out.signal_diag[HTF_GATE] = True
    out.signal_diag[RSI_CHASE_GATE] = True
    return out


def apply_llm_book_lock(decision: Decision, positions: Iterable[Any]) -> Decision:
    """
    One ticket per instrument: no scale-in, no opposite hedge.

    Disable with ``KEEL_LLM_NO_SCALE_IN=0``.
    """
    if not llm_book_lock_enabled():
        return decision
    if decision.action not in _FIRE_ACTIONS:
        return decision
    wanted = "long" if decision.action == "BUY_LONG" else "short"
    held_sides: set[str] = set()
    for pos in positions or []:
        inst = getattr(pos, "inst_id", None)
        if inst is None and isinstance(pos, dict):
            inst = pos.get("inst_id")
        if str(inst or "") != str(decision.inst_id):
            continue
        size = getattr(pos, "size", None)
        if size is None and isinstance(pos, dict):
            size = pos.get("size")
        try:
            if float(size or 0) <= 0:
                continue
        except (TypeError, ValueError):
            continue
        side = getattr(pos, "side", None)
        if side is None and isinstance(pos, dict):
            side = pos.get("side")
        side_s = str(side or "").strip().lower()
        if side_s in ("long", "short"):
            held_sides.add(side_s)
    if not held_sides:
        return decision
    if wanted in held_sides:
        return _wait(
            decision,
            "already in position — no scale-in",
            gate=BOOK_GATE,
            extra={"book_sides": sorted(held_sides), "book_ok": False},
        )
    return _wait(
        decision,
        f"opposite position open ({','.join(sorted(held_sides))}) — no hedge",
        gate=BOOK_GATE,
        extra={"book_sides": sorted(held_sides), "book_ok": False},
    )


__all__ = [
    "BOOK_GATE",
    "GEOMETRY_GATE",
    "HTF_GATE",
    "RSI_CHASE_GATE",
    "apply_llm_book_lock",
    "apply_llm_edge_overlay",
    "llm_book_lock_enabled",
    "llm_edge_overlay_enabled",
    "plan_llm_geometry",
]
