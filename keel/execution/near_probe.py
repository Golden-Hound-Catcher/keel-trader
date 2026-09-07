"""
Q3 shadow near-signal probe (rehearsal only).

When kill-switch + shadow mode + KEEL_SHADOW_NEAR_PROBE are all on, optionally
convert a strong WAIT near-signal into a shadow-only BUY_LONG / SELL_SHORT that
goes through ExecutionOrchestrator._shadow_fill — never exchange place_order.

Safety: without kill OR without shadow OR with probe off, this module returns
None (no conversion). Callers must still execute under shadow_mode.
"""
from __future__ import annotations

import time
from typing import Any

from keel.domain.decision import Decision, DecisionAction, validate_decision
from keel.factors.market_data import MarketSnapshot

# Ledger / audit tag — distinguish probe fills from forced/manual shadow fills.
PROBE_POLICY = "shadow_near_probe"
PROBE_STRATEGY_TAG = "keel-shadow-near-probe"

# Align with notify near-signal alert: nearest in {long,short} and len(missing)<=2.
DEFAULT_MAX_MISSING = 2


def near_signal_meets_gates(
    diag: dict[str, Any] | None,
    *,
    max_missing: int = DEFAULT_MAX_MISSING,
    min_confidence: float = 0.0,
    decision_confidence: float = 0.0,
) -> bool:
    """True when signal_diag is a strong near-signal under configured gates."""
    if not isinstance(diag, dict):
        return False
    nearest = diag.get("nearest")
    if nearest not in ("long", "short"):
        return False
    missing_raw = diag.get("missing")
    missing = missing_raw if isinstance(missing_raw, list) else []
    if len(missing) > int(max_missing):
        return False
    try:
        conf = float(decision_confidence)
    except (TypeError, ValueError):
        conf = 0.0
    if conf < float(min_confidence):
        return False
    return True


def probe_action_for_nearest(nearest: str) -> DecisionAction | None:
    """Map signal_diag.nearest → shadow action."""
    if nearest == "long":
        return "BUY_LONG"
    if nearest == "short":
        return "SELL_SHORT"
    return None


def should_attempt_near_probe(
    *,
    kill_switch: bool,
    shadow_mode: bool,
    probe_enabled: bool,
) -> bool:
    """
    Probe only when kill + shadow + probe flag are all on.

    Missing any of the three → no conversion (and never a live order path).
    """
    return bool(kill_switch) and bool(shadow_mode) and bool(probe_enabled)


def probe_fill_recent(
    ledger: Any,
    *,
    inst_id: str,
    cooldown_seconds: float,
    now: float | None = None,
) -> bool:
    """True when a probe-tagged shadow_fill for inst_id is inside the cooldown window."""
    if cooldown_seconds <= 0:
        return False
    if ledger is None:
        return False
    stamp = float(now if now is not None else time.time())
    cutoff = stamp - float(cooldown_seconds)
    try:
        events = ledger.get_events(event_type="shadow_fill", inst_id=inst_id, limit=20)
    except Exception:
        return False
    for ev in events or []:
        ts = getattr(ev, "timestamp", None)
        try:
            if ts is None or float(ts) < cutoff:
                continue
        except (TypeError, ValueError):
            continue
        data = getattr(ev, "data", None) or {}
        if not isinstance(data, dict):
            continue
        if data.get("policy") == PROBE_POLICY or data.get("probe") is True:
            return True
        reason = str(data.get("reason") or "")
        if reason.startswith(PROBE_POLICY):
            return True
    return False


def build_near_probe_decision(
    wait_decision: Decision,
    snapshot: MarketSnapshot,
) -> Decision | None:
    """
    Build a fillable shadow Decision from a WAIT near-signal + market snapshot.

    Returns None when geometry cannot be built. Does not check kill/shadow/probe
    flags — callers must gate with ``should_attempt_near_probe`` first.
    """
    if wait_decision.action != "WAIT":
        return None
    diag = wait_decision.signal_diag if isinstance(wait_decision.signal_diag, dict) else None
    if not diag:
        return None
    nearest = diag.get("nearest")
    action = probe_action_for_nearest(str(nearest) if nearest is not None else "")
    if action is None:
        return None

    price = float(snapshot.price or 0.0)
    atr = float(snapshot.atr_14 or 0.0)
    if price <= 0 or atr <= 0:
        return None

    missing = diag.get("missing") if isinstance(diag.get("missing"), list) else []
    margin = 50.0
    reason = (
        f"{PROBE_POLICY}: nearest={nearest} missing={len(missing)} "
        f"(rehearsal only; never live)"
    )

    if action == "BUY_LONG":
        entry = price
        sl = entry - 1.0 * atr
        tp = entry + 2.2 * atr
    else:
        entry = price
        sl = entry + 1.0 * atr
        tp = entry - 2.2 * atr

    return validate_decision(
        Decision(
            inst_id=wait_decision.inst_id,
            action=action,
            confidence=max(float(wait_decision.confidence or 0.0), 60.0),
            entry_price=entry,
            take_profit=tp,
            stop_loss=sl,
            leverage=3,
            margin_usdt=margin,
            reason=reason,
            signal_diag=diag,
        )
    )


def maybe_near_probe_decision(
    decision: Decision,
    snapshot: MarketSnapshot,
    *,
    kill_switch: bool,
    shadow_mode: bool,
    probe_enabled: bool,
    max_missing: int = DEFAULT_MAX_MISSING,
    min_confidence: float = 0.0,
    cooldown_seconds: float = 900.0,
    ledger: Any | None = None,
    now: float | None = None,
) -> Decision | None:
    """
    Optionally convert WAIT + strong near-signal → shadow probe Decision.

    Returns the probe Decision when all gates pass, else None (caller keeps WAIT).
    """
    if not should_attempt_near_probe(
        kill_switch=kill_switch,
        shadow_mode=shadow_mode,
        probe_enabled=probe_enabled,
    ):
        return None
    if decision.action != "WAIT":
        return None
    if not near_signal_meets_gates(
        decision.signal_diag,
        max_missing=max_missing,
        min_confidence=min_confidence,
        decision_confidence=decision.confidence,
    ):
        return None
    if probe_fill_recent(
        ledger,
        inst_id=decision.inst_id,
        cooldown_seconds=cooldown_seconds,
        now=now,
    ):
        return None
    probed = build_near_probe_decision(decision, snapshot)
    if probed is None or not probed.valid or probed.action == "WAIT":
        return None
    return probed


def is_probe_decision(decision: Decision) -> bool:
    """True when decision was synthesized by the near-signal probe."""
    reason = str(decision.reason or "")
    return reason.startswith(PROBE_POLICY)
