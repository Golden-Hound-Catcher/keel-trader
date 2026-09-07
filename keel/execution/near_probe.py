"""
Q3 shadow near-signal probe (rehearsal only).

When kill-switch + shadow mode + KEEL_SHADOW_NEAR_PROBE are all on, optionally
convert a strong WAIT near-signal into a shadow-only BUY_LONG / SELL_SHORT that
goes through ExecutionOrchestrator._shadow_fill — never exchange place_order.

Q3.4: fee-aware minimum edge hurdle (OKX role fee / optional override) so weak
near-signals that cannot clear trading fees do not emit shadow_fill.

Safety: without kill OR without shadow OR with probe off, this module returns
None (no conversion). Callers must still execute under shadow_mode.
"""
from __future__ import annotations

import time
from typing import Any, Literal

from keel.domain.decision import Decision, DecisionAction, validate_decision
from keel.factors.market_data import MarketSnapshot

# Ledger / audit tag — distinguish probe fills from forced/manual shadow fills.
PROBE_POLICY = "shadow_near_probe"
PROBE_STRATEGY_TAG = "keel-shadow-near-probe"

# Align with notify near-signal alert: nearest in {long,short} and len(missing)<=2.
DEFAULT_MAX_MISSING = 2

# Rule policy gate counts (long/short each have 5 gates) — used for completeness.
RULE_GATE_COUNT = 5

# Probe geometry mirrors rule_based_decision: TP = 2.2 ATR, SL = 1.0 ATR.
_PROBE_TP_ATR = 2.2
_PROBE_SL_ATR = 1.0

EdgeMode = Literal["round_trip", "open"]

# Q3.5: durable ledger event when near-probe evaluates but does not fire.
SKIP_EVENT_TYPE = "shadow_near_probe_skip"

SkipReason = Literal[
    "below_hurdle",
    "edge_unavailable",
    "cooldown",
    "max_missing",
    "probe_disabled",
    "not_near",
]

SKIP_REASONS: tuple[str, ...] = (
    "below_hurdle",
    "edge_unavailable",
    "cooldown",
    "max_missing",
    "probe_disabled",
    "not_near",
)


class NearProbeOutcome:
    """Result of evaluating near-probe: either a Decision or a skip reason."""

    __slots__ = ("decision", "skip_reason", "edge_bps", "hurdle_bps", "fee_role", "edge_mode")

    def __init__(
        self,
        decision: Decision | None = None,
        *,
        skip_reason: str | None = None,
        edge_bps: float | None = None,
        hurdle_bps: float | None = None,
        fee_role: str | None = None,
        edge_mode: str | None = None,
    ) -> None:
        self.decision = decision
        self.skip_reason = skip_reason
        self.edge_bps = edge_bps
        self.hurdle_bps = hurdle_bps
        self.fee_role = fee_role
        self.edge_mode = edge_mode

    @property
    def fired(self) -> bool:
        return self.decision is not None


def near_probe_skip_payload(
    reason: str,
    *,
    edge_bps: float | None = None,
    hurdle_bps: float | None = None,
    fee_role: str | None = None,
    edge_mode: str | None = None,
    inst_id: str | None = None,
) -> dict[str, Any]:
    """Lightweight ledger/API payload for a near-probe skip."""
    data: dict[str, Any] = {"reason": str(reason)}
    if edge_bps is not None:
        data["edge_bps"] = float(edge_bps)
    if hurdle_bps is not None:
        data["hurdle_bps"] = float(hurdle_bps)
    if fee_role:
        data["fee_role"] = str(fee_role)
    if edge_mode:
        data["edge_mode"] = str(edge_mode)
    if inst_id:
        data["inst_id"] = str(inst_id)
    return data


def record_near_probe_skip(
    ledger: Any,
    *,
    inst_id: str,
    reason: str,
    edge_bps: float | None = None,
    hurdle_bps: float | None = None,
    fee_role: str | None = None,
    edge_mode: str | None = None,
    timestamp: float | None = None,
) -> int | None:
    """Write ``shadow_near_probe_skip`` ledger event. Returns event id or None."""
    if ledger is None:
        return None
    payload = near_probe_skip_payload(
        reason,
        edge_bps=edge_bps,
        hurdle_bps=hurdle_bps,
        fee_role=fee_role,
        edge_mode=edge_mode,
    )
    try:
        return int(
            ledger.record_event(
                SKIP_EVENT_TYPE,
                inst_id=inst_id,
                data=payload,
                timestamp=timestamp,
            )
        )
    except Exception:
        return None


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


def normalize_edge_mode(raw: str | None) -> EdgeMode:
    """round_trip (default) | open."""
    s = str(raw or "round_trip").strip().lower()
    return "open" if s == "open" else "round_trip"


def resolve_near_probe_hurdle_bps(
    settings: Any | None = None,
    *,
    min_edge_override: float | None = None,
    edge_mode: str | None = None,
    fee_role: str | None = None,
) -> tuple[float, str, str]:
    """
    Resolve fee-aware edge hurdle in bps.

    Returns ``(hurdle_bps, fee_role, edge_mode)``.
    Explicit ``min_edge_override`` (or settings.shadow_near_probe_min_edge_bps)
    wins; else open_fee_bps or round_trip_fee_bps from OKX fee model for role.
    """
    if settings is None:
        try:
            from keel.config import get_settings

            settings = get_settings()
        except Exception:
            settings = None

    override = min_edge_override
    if override is None and settings is not None:
        override = getattr(settings, "shadow_near_probe_min_edge_bps", None)

    mode_raw = edge_mode
    if mode_raw is None and settings is not None:
        mode_raw = getattr(settings, "shadow_near_probe_edge_mode", None)
    mode = normalize_edge_mode(mode_raw)

    role_raw = fee_role
    if role_raw is None and settings is not None:
        role_raw = getattr(settings, "shadow_fee_role", None)
    role = "maker" if str(role_raw or "taker").strip().lower() == "maker" else "taker"

    if override is not None:
        return float(override), role, mode

    from keel.exchange.okx_fees import build_fee_model

    fee_model = build_fee_model(settings)
    # Prefer role from fee_model (normalized).
    role = str(fee_model.get("role") or role)
    if mode == "open":
        hurdle = float(fee_model.get("open_fee_bps") or 0.0)
    else:
        hurdle = float(fee_model.get("round_trip_fee_bps") or 0.0)
    return hurdle, role, mode


def estimate_near_probe_edge_bps(
    diag: dict[str, Any] | None,
    snapshot: MarketSnapshot | None,
    *,
    decision_confidence: float = 0.0,
) -> float | None:
    """
    Conservative expected-edge estimate in bps from near-signal geometry.

    Uses ATR/price as move scale and an EV vs probe TP/SL (2.2 / 1.0 ATR)
    with win probability ≈ gate_completeness × confidence/100.

    Returns ``None`` when not estimable (caller should fail closed when hurdle > 0).
    """
    if snapshot is None:
        return None
    try:
        price = float(snapshot.price or 0.0)
        atr = float(snapshot.atr_14 or 0.0)
    except (TypeError, ValueError):
        return None
    if price <= 0 or atr <= 0:
        return None

    missing: list[Any] = []
    if isinstance(diag, dict):
        raw = diag.get("missing")
        if isinstance(raw, list):
            missing = raw
    n_missing = len(missing)
    completeness = max(0.0, min(1.0, (RULE_GATE_COUNT - n_missing) / float(RULE_GATE_COUNT)))

    try:
        conf = float(decision_confidence)
    except (TypeError, ValueError):
        conf = 0.0
    conf_factor = max(0.0, min(1.0, conf / 100.0))

    # Crude win probability from how complete + confident the near-signal is.
    p = completeness * conf_factor
    # EV in ATR units for probe geometry (reward 2.2 ATR, risk 1.0 ATR).
    ev_atr = (_PROBE_TP_ATR * p) - (_PROBE_SL_ATR * (1.0 - p))
    atr_bps = (atr / price) * 10_000.0
    edge = atr_bps * ev_atr
    # Negative EV → treat as 0 edge (will not clear a positive fee hurdle).
    return float(max(0.0, edge))


def edge_clears_hurdle(edge_bps: float | None, hurdle_bps: float) -> bool:
    """
    True when estimated edge clears the fee hurdle.

    ``hurdle_bps <= 0`` disables the gate (explicit override 0).
    When hurdle > 0 and edge is None → fail closed.
    """
    if float(hurdle_bps) <= 0.0:
        return True
    if edge_bps is None:
        return False
    return float(edge_bps) >= float(hurdle_bps)


def build_near_probe_decision(
    wait_decision: Decision,
    snapshot: MarketSnapshot,
    *,
    edge_bps: float | None = None,
    hurdle_bps: float | None = None,
    fee_role: str | None = None,
    edge_mode: str | None = None,
) -> Decision | None:
    """
    Build a fillable shadow Decision from a WAIT near-signal + market snapshot.

    Returns None when geometry cannot be built. Does not check kill/shadow/probe
    flags — callers must gate with ``should_attempt_near_probe`` first.
    Optional edge/hurdle/fee_role are stamped into reason + signal_diag for audit.
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
    reason_parts = [
        f"{PROBE_POLICY}: nearest={nearest} missing={len(missing)}",
    ]
    if edge_bps is not None:
        reason_parts.append(f"edge_bps={edge_bps:.4g}")
    if hurdle_bps is not None:
        reason_parts.append(f"hurdle_bps={hurdle_bps:.4g}")
    if fee_role:
        reason_parts.append(f"fee_role={fee_role}")
    if edge_mode:
        reason_parts.append(f"edge_mode={edge_mode}")
    reason_parts.append("(rehearsal only; never live)")
    reason = " ".join(reason_parts)

    # Copy diag and stamp probe audit fields for ledger / Monitor.
    audit_diag = dict(diag)
    if edge_bps is not None:
        audit_diag["edge_bps"] = float(edge_bps)
    if hurdle_bps is not None:
        audit_diag["hurdle_bps"] = float(hurdle_bps)
    if fee_role:
        audit_diag["fee_role"] = str(fee_role)
    if edge_mode:
        audit_diag["edge_mode"] = str(edge_mode)

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
            signal_diag=audit_diag,
        )
    )


def evaluate_near_probe(
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
    min_edge_bps: float | None = None,
    edge_mode: str | None = None,
    fee_role: str | None = None,
    settings: Any | None = None,
) -> NearProbeOutcome:
    """
    Evaluate near-probe gates and return Decision or a typed skip reason.

    Skip reasons (Q3.5): ``probe_disabled``, ``not_near``, ``max_missing``,
    ``cooldown``, ``edge_unavailable``, ``below_hurdle``.
    """
    if not should_attempt_near_probe(
        kill_switch=kill_switch,
        shadow_mode=shadow_mode,
        probe_enabled=probe_enabled,
    ):
        return NearProbeOutcome(skip_reason="probe_disabled")

    if decision.action != "WAIT":
        return NearProbeOutcome(skip_reason="not_near")

    diag = decision.signal_diag if isinstance(decision.signal_diag, dict) else None
    if not isinstance(diag, dict):
        return NearProbeOutcome(skip_reason="not_near")
    nearest = diag.get("nearest")
    if nearest not in ("long", "short"):
        return NearProbeOutcome(skip_reason="not_near")

    missing_raw = diag.get("missing")
    missing = missing_raw if isinstance(missing_raw, list) else []
    if len(missing) > int(max_missing):
        return NearProbeOutcome(skip_reason="max_missing")

    try:
        conf = float(decision.confidence)
    except (TypeError, ValueError):
        conf = 0.0
    if conf < float(min_confidence):
        # Below configured confidence — treat as not strong-near.
        return NearProbeOutcome(skip_reason="not_near")

    if probe_fill_recent(
        ledger,
        inst_id=decision.inst_id,
        cooldown_seconds=cooldown_seconds,
        now=now,
    ):
        return NearProbeOutcome(skip_reason="cooldown")

    hurdle, role, mode = resolve_near_probe_hurdle_bps(
        settings,
        min_edge_override=min_edge_bps,
        edge_mode=edge_mode,
        fee_role=fee_role,
    )
    edge = estimate_near_probe_edge_bps(
        diag,
        snapshot,
        decision_confidence=decision.confidence,
    )
    if float(hurdle) > 0.0 and edge is None:
        return NearProbeOutcome(
            skip_reason="edge_unavailable",
            edge_bps=None,
            hurdle_bps=hurdle,
            fee_role=role,
            edge_mode=mode,
        )
    if not edge_clears_hurdle(edge, hurdle):
        return NearProbeOutcome(
            skip_reason="below_hurdle",
            edge_bps=edge,
            hurdle_bps=hurdle,
            fee_role=role,
            edge_mode=mode,
        )

    probed = build_near_probe_decision(
        decision,
        snapshot,
        edge_bps=edge,
        hurdle_bps=hurdle,
        fee_role=role,
        edge_mode=mode,
    )
    if probed is None or not probed.valid or probed.action == "WAIT":
        return NearProbeOutcome(
            skip_reason="edge_unavailable",
            edge_bps=edge,
            hurdle_bps=hurdle,
            fee_role=role,
            edge_mode=mode,
        )
    return NearProbeOutcome(
        probed,
        edge_bps=edge,
        hurdle_bps=hurdle,
        fee_role=role,
        edge_mode=mode,
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
    min_edge_bps: float | None = None,
    edge_mode: str | None = None,
    fee_role: str | None = None,
    settings: Any | None = None,
) -> Decision | None:
    """
    Optionally convert WAIT + strong near-signal → shadow probe Decision.

    Returns the probe Decision when all gates pass (incl. fee edge hurdle),
    else None (caller keeps WAIT). Prefer ``evaluate_near_probe`` when skip
    reasons are needed (Q3.5).
    """
    return evaluate_near_probe(
        decision,
        snapshot,
        kill_switch=kill_switch,
        shadow_mode=shadow_mode,
        probe_enabled=probe_enabled,
        max_missing=max_missing,
        min_confidence=min_confidence,
        cooldown_seconds=cooldown_seconds,
        ledger=ledger,
        now=now,
        min_edge_bps=min_edge_bps,
        edge_mode=edge_mode,
        fee_role=fee_role,
        settings=settings,
    ).decision


def is_probe_decision(decision: Decision) -> bool:
    """True when decision was synthesized by the near-signal probe."""
    reason = str(decision.reason or "")
    return reason.startswith(PROBE_POLICY)


def probe_audit_fields(decision: Decision) -> dict[str, Any]:
    """Extract edge/hurdle/fee_role audit fields from a probe Decision."""
    out: dict[str, Any] = {}
    diag = decision.signal_diag if isinstance(decision.signal_diag, dict) else {}
    for key in ("edge_bps", "hurdle_bps", "fee_role", "edge_mode"):
        if key in diag and diag[key] is not None:
            out[key] = diag[key]
    return out
