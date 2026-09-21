"""
Hard edge overlay for ``KEEL_DECISION_POLICY=llm``.

Kernel (not the model):
- WAIT unless 1h agrees with the side; 4h is soft by default (F8):
  neutral OK if 1h aligned; still block if 4h opposes. Hard mode =
  same-direction only (``KEEL_LLM_4H_MODE=hard``).
- F10 soft-4h + 15m: when soft and 4h is **neutral**, require 15m
  **same-direction** (gate ``soft4h_needs_15m``); else 15m not-opposing (F7)
- Optional 15m not-opposing (F7): short → 15m≠bullish; long → 15m≠bearish
- Optional ADX floor (F7/F8, default 15; 0=off; fail-open if ADX unavailable)
- RSI chase veto (F10: long>65 / short<35; was 70/30)
- RSI mid-range veto (F7/F8): short if RSI≥52; long if RSI≤48
- Min confidence (F7/F8 default 60; llm_demo profile may raise to 65)
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
from keel.factors.technical import calculate_adx

_FIRE_ACTIONS = frozenset({"BUY_LONG", "SELL_SHORT"})
HTF_GATE = "htf_ok"
TF15_GATE = "tf15_align_ok"
SOFT4H_15M_GATE = "soft4h_needs_15m"
ADX_GATE = "adx_ok"
RSI_CHASE_GATE = "rsi_chase_ok"
RSI_MID_GATE = "rsi_mid_ok"
CONFIDENCE_GATE = "confidence_ok"
GEOMETRY_GATE = "geometry_ok"
BOOK_GATE = "book_ok"

DEFAULT_SL_ATR = 2.0
DEFAULT_TP_RR = 2.2
DEFAULT_RT_FEE_BPS = 10.0
DEFAULT_FEE_SL_MULT = 6.0
DEFAULT_FEE_TP_MULT = 12.0
DEFAULT_ADX_MIN = 15.0
DEFAULT_ADX_PERIOD = 14
DEFAULT_SHORT_RSI_MAX = 52.0
DEFAULT_LONG_RSI_MIN = 48.0
DEFAULT_MIN_CONFIDENCE = 60.0
# F10: tighter chase than classic 70/30 (still looser than mid veto).
DEFAULT_RSI_CHASE_LONG_MAX = 65.0
DEFAULT_RSI_CHASE_SHORT_MIN = 35.0


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


def _4h_mode() -> str:
    """
    F8: how to interpret 4h when ``KEEL_LLM_REQUIRE_4H=1``.

    - ``soft`` (default): 4h neutral OK if 1h aligns; block only if 4h opposes.
    - ``hard``: 4h must match side (pre-F8 / F7 behavior).
    """
    raw = (_env("KEEL_LLM_4H_MODE", "") or "").strip().lower()
    if raw in ("hard", "strict", "same"):
        return "hard"
    if raw in ("soft", "not_oppose", "not-opposing", "neutral_ok"):
        return "soft"
    return "soft"


def _require_15m_align() -> bool:
    """F7: entry TF must not oppose the side. Default on."""
    return _flag("KEEL_LLM_REQUIRE_15M_ALIGN", True)


def _adx_min() -> float:
    """F7/F8: ADX floor; 0 disables. Default 15."""
    return max(0.0, _num("KEEL_LLM_ADX_MIN", DEFAULT_ADX_MIN))


def _adx_period() -> int:
    return max(5, min(50, int(_num("KEEL_LLM_ADX_PERIOD", float(DEFAULT_ADX_PERIOD)))))


def _short_rsi_max() -> float:
    return _num("KEEL_LLM_SHORT_RSI_MAX", DEFAULT_SHORT_RSI_MAX)


def _long_rsi_min() -> float:
    return _num("KEEL_LLM_LONG_RSI_MIN", DEFAULT_LONG_RSI_MIN)


def _min_confidence() -> float:
    return max(0.0, _num("KEEL_LLM_MIN_CONFIDENCE", DEFAULT_MIN_CONFIDENCE))


def _rsi_chase_long_max() -> float:
    return _num("KEEL_LLM_RSI_CHASE_LONG_MAX", DEFAULT_RSI_CHASE_LONG_MAX)


def _rsi_chase_short_min() -> float:
    return _num("KEEL_LLM_RSI_CHASE_SHORT_MIN", DEFAULT_RSI_CHASE_SHORT_MIN)


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
    """
    1h must align with side (when required).

    4h (when ``KEEL_LLM_REQUIRE_4H=1``):
    - soft (default, F8): allow neutral; block only when 4h opposes the side.
    - hard: 4h must match side (bullish for long / bearish for short).
    """
    t1h = _trend(snapshot, "trend_1h")
    t4h = _trend(snapshot, "trend_4h")
    need_1h = _require_1h()
    need_4h = _require_4h()
    mode = _4h_mode() if need_4h else "off"
    if side == "long":
        ok_1h = (not need_1h) or t1h == "bullish"
        if not need_4h:
            ok_4h = True
        elif mode == "hard":
            ok_4h = t4h == "bullish"
        else:
            # soft: not-opposing (neutral OK; bearish blocks long)
            ok_4h = t4h != "bearish"
    else:
        ok_1h = (not need_1h) or t1h == "bearish"
        if not need_4h:
            ok_4h = True
        elif mode == "hard":
            ok_4h = t4h == "bearish"
        else:
            ok_4h = t4h != "bullish"
    audit = {
        "trend_1h": t1h,
        "trend_4h": t4h,
        "require_1h_trend": need_1h,
        "require_4h_trend": need_4h,
        "llm_4h_mode": mode,
        "htf_ok": bool(ok_1h and ok_4h),
    }
    return bool(ok_1h and ok_4h), audit


def _tf15_align_ok(snapshot: MarketSnapshot, side: str) -> tuple[bool, dict[str, Any]]:
    """
    F7: 15m must not oppose the side.

    short → trend_15m ≠ bullish; long → trend_15m ≠ bearish.
    Neutral is allowed. Disabled when ``KEEL_LLM_REQUIRE_15M_ALIGN=0``.
    (F10 soft-4h same-dir confirm is a separate gate — see ``_soft4h_15m_ok``.)
    """
    t15 = _trend(snapshot, "trend_15m")
    require = _require_15m_align()
    if not require:
        ok = True
    elif side == "long":
        ok = t15 != "bearish"
    else:
        ok = t15 != "bullish"
    audit = {
        "trend_15m": t15,
        "require_15m_align": require,
        TF15_GATE: bool(ok),
    }
    return bool(ok), audit


def _soft4h_15m_ok(snapshot: MarketSnapshot, side: str) -> tuple[bool, dict[str, Any]]:
    """
    F10: when ``KEEL_LLM_4H_MODE=soft`` and 4h is **neutral**, require 15m
    **same-direction** as the side (not merely not-opposing).

    - Applies only when 4h require is on, mode is soft, and trend_4h == neutral.
    - long → trend_15m == bullish; short → trend_15m == bearish.
    - Neutral or opposing 15m → WAIT with gate ``soft4h_needs_15m``.
    - When 4h already aligns (or hard/off), this gate is a no-op (ok=True).
    """
    need_4h = _require_4h()
    mode = _4h_mode() if need_4h else "off"
    t4h = _trend(snapshot, "trend_4h")
    t15 = _trend(snapshot, "trend_15m")
    applies = bool(need_4h and mode == "soft" and t4h == "neutral")
    if not applies:
        return True, {
            "soft4h_15m_applies": False,
            "trend_4h": t4h,
            "trend_15m": t15,
            "llm_4h_mode": mode,
            SOFT4H_15M_GATE: True,
        }
    if side == "long":
        ok = t15 == "bullish"
    else:
        ok = t15 == "bearish"
    return bool(ok), {
        "soft4h_15m_applies": True,
        "trend_4h": t4h,
        "trend_15m": t15,
        "llm_4h_mode": mode,
        SOFT4H_15M_GATE: bool(ok),
    }


def _snapshot_adx(snapshot: MarketSnapshot) -> tuple[float | None, str]:
    """
    Prefer an ADX already on the snapshot; else compute from 15m candles.

    Returns ``(adx_or_None, source)`` where source is
    ``snapshot`` | ``computed`` | ``unavailable``.
    Fail-open callers treat unavailable as skip (not a hard block).
    """
    for attr in ("adx_14", "adx", "adx_value"):
        raw = getattr(snapshot, attr, None)
        if raw is None:
            continue
        try:
            v = float(raw)
        except (TypeError, ValueError):
            continue
        if v > 0.0:
            return v, "snapshot"
    candles = list(getattr(snapshot, "candles_15m", None) or [])
    if len(candles) < 2:
        return None, "unavailable"
    try:
        highs = [float(c.high) for c in candles]
        lows = [float(c.low) for c in candles]
        closes = [float(c.close) for c in candles]
        res = calculate_adx(highs, lows, closes, period=_adx_period())
        v = float(res.adx)
        if v > 0.0:
            return v, "computed"
        return None, "unavailable"
    except Exception:
        return None, "unavailable"


def _adx_ok(snapshot: MarketSnapshot) -> tuple[bool, dict[str, Any]]:
    """
    F7 ADX floor. ``KEEL_LLM_ADX_MIN=0`` disables.

    When min > 0 but ADX cannot be read/computed → **fail-open** (ok=True)
    with ``adx_fail_open=True`` stamped for audit.
    """
    amin = _adx_min()
    period = _adx_period()
    if amin <= 0.0:
        return True, {
            "adx": None,
            "adx_min": amin,
            "adx_period": period,
            "adx_enabled": False,
            "adx_source": "off",
            "adx_fail_open": False,
            ADX_GATE: True,
        }
    adx_v, source = _snapshot_adx(snapshot)
    if adx_v is None:
        return True, {
            "adx": None,
            "adx_min": amin,
            "adx_period": period,
            "adx_enabled": True,
            "adx_source": source,
            "adx_fail_open": True,
            ADX_GATE: True,
            "adx_note": "ADX unavailable — fail-open (documented F7)",
        }
    ok = adx_v >= float(amin)
    return bool(ok), {
        "adx": adx_v,
        "adx_min": amin,
        "adx_period": period,
        "adx_enabled": True,
        "adx_source": source,
        "adx_fail_open": False,
        ADX_GATE: bool(ok),
    }


def _rsi_chase_ok(snapshot: MarketSnapshot, side: str) -> tuple[bool, dict[str, Any]]:
    """F10 chase veto: long RSI > long_max / short RSI < short_min → WAIT."""
    rsi = float(getattr(snapshot, "rsi_14", 50.0) or 50.0)
    long_max = _rsi_chase_long_max()
    short_min = _rsi_chase_short_min()
    if side == "long":
        ok = rsi <= float(long_max)
    else:
        ok = rsi >= float(short_min)
    return bool(ok), {
        "rsi_14": rsi,
        "rsi_chase_long_max": long_max,
        "rsi_chase_short_min": short_min,
        RSI_CHASE_GATE: bool(ok),
    }


def _rsi_mid_ok(snapshot: MarketSnapshot, side: str) -> tuple[bool, dict[str, Any]]:
    """
    F7 mid-range veto (tighter than chase 70/30).

    Short blocked if RSI ≥ ``KEEL_LLM_SHORT_RSI_MAX`` (default 52).
    Long blocked if RSI ≤ ``KEEL_LLM_LONG_RSI_MIN`` (default 48).
    """
    rsi = float(getattr(snapshot, "rsi_14", 50.0) or 50.0)
    short_max = _short_rsi_max()
    long_min = _long_rsi_min()
    if side == "short":
        ok = rsi < float(short_max)
    else:
        ok = rsi > float(long_min)
    audit = {
        "rsi_14": rsi,
        "short_rsi_max": short_max,
        "long_rsi_min": long_min,
        RSI_MID_GATE: bool(ok),
    }
    return bool(ok), audit


def _confidence_ok(decision: Decision) -> tuple[bool, dict[str, Any]]:
    """F7/F8: WAIT when model confidence < ``KEEL_LLM_MIN_CONFIDENCE`` (default 60)."""
    conf = float(decision.confidence or 0.0)
    floor = _min_confidence()
    ok = conf >= floor
    return bool(ok), {
        "confidence": conf,
        "min_confidence": floor,
        CONFIDENCE_GATE: bool(ok),
    }


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
    audit: dict[str, Any] = {}

    htf_ok, htf_audit = _htf_ok(snapshot, side)
    audit.update(htf_audit)
    if not htf_ok:
        return _wait(
            decision,
            (
                f"htf {side} needs 1h align + 4h "
                f"{htf_audit.get('llm_4h_mode', 'soft')} "
                f"(t1h={htf_audit['trend_1h']} t4h={htf_audit['trend_4h']})"
            ),
            gate=HTF_GATE,
            extra=audit,
        )

    soft4h_ok, soft4h_audit = _soft4h_15m_ok(snapshot, side)
    audit.update(soft4h_audit)
    if not soft4h_ok:
        return _wait(
            decision,
            (
                f"soft4h needs 15m same-dir {side} "
                f"(t4h={soft4h_audit['trend_4h']} t15={soft4h_audit['trend_15m']})"
            ),
            gate=SOFT4H_15M_GATE,
            extra={**audit, HTF_GATE: True},
        )

    tf15_ok, tf15_audit = _tf15_align_ok(snapshot, side)
    audit.update(tf15_audit)
    if not tf15_ok:
        return _wait(
            decision,
            f"15m opposing {side} (t15={tf15_audit['trend_15m']})",
            gate=TF15_GATE,
            extra={**audit, HTF_GATE: True, SOFT4H_15M_GATE: True},
        )

    adx_pass, adx_audit = _adx_ok(snapshot)
    audit.update(adx_audit)
    if not adx_pass:
        return _wait(
            decision,
            f"adx floor veto adx={adx_audit.get('adx')} min={adx_audit.get('adx_min')}",
            gate=ADX_GATE,
            extra={**audit, HTF_GATE: True, SOFT4H_15M_GATE: True, TF15_GATE: True},
        )

    chase_ok, chase_audit = _rsi_chase_ok(snapshot, side)
    audit.update(chase_audit)
    if not chase_ok:
        rsi = float(chase_audit.get("rsi_14") or 0.0)
        return _wait(
            decision,
            f"rsi chase veto rsi={rsi:.1f}",
            gate=RSI_CHASE_GATE,
            extra={
                **audit,
                HTF_GATE: True,
                SOFT4H_15M_GATE: True,
                TF15_GATE: True,
                ADX_GATE: True,
            },
        )

    mid_ok, mid_audit = _rsi_mid_ok(snapshot, side)
    audit.update(mid_audit)
    if not mid_ok:
        return _wait(
            decision,
            f"rsi mid veto rsi={mid_audit['rsi_14']:.1f} side={side}",
            gate=RSI_MID_GATE,
            extra={
                **audit,
                HTF_GATE: True,
                SOFT4H_15M_GATE: True,
                TF15_GATE: True,
                ADX_GATE: True,
                RSI_CHASE_GATE: True,
            },
        )

    conf_ok, conf_audit = _confidence_ok(decision)
    audit.update(conf_audit)
    if not conf_ok:
        return _wait(
            decision,
            f"confidence {conf_audit['confidence']:.0f} < min {conf_audit['min_confidence']:.0f}",
            gate=CONFIDENCE_GATE,
            extra={
                **audit,
                HTF_GATE: True,
                SOFT4H_15M_GATE: True,
                TF15_GATE: True,
                ADX_GATE: True,
                RSI_CHASE_GATE: True,
                RSI_MID_GATE: True,
            },
        )

    out = _rewrite_geometry(decision, snapshot)
    if out.signal_diag is None:
        out.signal_diag = {}
    out.signal_diag.update(audit)
    out.signal_diag[HTF_GATE] = True
    out.signal_diag[SOFT4H_15M_GATE] = True
    out.signal_diag[TF15_GATE] = True
    out.signal_diag[ADX_GATE] = True
    out.signal_diag[RSI_CHASE_GATE] = True
    out.signal_diag[RSI_MID_GATE] = True
    out.signal_diag[CONFIDENCE_GATE] = True
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
    "ADX_GATE",
    "BOOK_GATE",
    "CONFIDENCE_GATE",
    "GEOMETRY_GATE",
    "HTF_GATE",
    "RSI_CHASE_GATE",
    "RSI_MID_GATE",
    "SOFT4H_15M_GATE",
    "TF15_GATE",
    "apply_llm_book_lock",
    "apply_llm_edge_overlay",
    "llm_book_lock_enabled",
    "llm_edge_overlay_enabled",
    "plan_llm_geometry",
]
