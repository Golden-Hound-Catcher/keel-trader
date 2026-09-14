"""
E3 per-instrument fire cooldown (rule full-gate + pure LLM fires).

After a cooldown-eligible BUY_LONG / SELL_SHORT is recorded, suppress
another same-instrument entry for ``KEEL_RULE_FIRE_COOLDOWN_SECONDS``
(default 900). Eligible rows:

- ``rule`` / ``llm_veto`` / unlabeled: full-gate only (``missing==[]``)
- ``llm``: any BUY_LONG / SELL_SHORT (model-as-trader has no missing=[])

Do not fold ``llm`` into ``RULE_POLICY_NAMES`` — that would pollute E1
full-gate stats.

During cooldown the decision becomes WAIT: signal_diag is kept for audit,
but ``nearest`` is neutralized and ``fire_cooldown_ok`` is appended to
``missing`` so it must NOT count as a full_gate_fire and must NOT produce
a near-probe shadow fill.

Uses ledger recent decisions — same pattern as near_probe cooldown.
"""
from __future__ import annotations

import time
from typing import Any

from keel.domain.decision import (
    SUPPRESS_COOLDOWN_GATE,
    Decision,
    neutralize_suppressed_fire_diag,
)
from keel.config.settings import _env_int
from keel.ledger.full_gate import is_full_gate_fire
from keel.ledger.near_entry_markout import signal_diag_from_calculus

RULE_FIRE_COOLDOWN_DEFAULT = 900
RULE_FIRE_COOLDOWN_MIN = 0
RULE_FIRE_COOLDOWN_MAX = 7200
LLM_REENTRY_DEFAULT = 3600
_FIRE_ACTIONS = frozenset({"BUY_LONG", "SELL_SHORT"})
# Pure LLM has no signal_diag.missing=[]; still spray-guard like a full-gate.
LLM_COOLDOWN_POLICIES = frozenset({"llm"})


def clamp_rule_fire_cooldown_seconds(raw: int | float | None) -> int:
    """Clamp KEEL_RULE_FIRE_COOLDOWN_SECONDS into [0, 7200]; 0 disables."""
    try:
        v = int(raw) if raw is not None else RULE_FIRE_COOLDOWN_DEFAULT
    except (TypeError, ValueError):
        v = RULE_FIRE_COOLDOWN_DEFAULT
    if v < RULE_FIRE_COOLDOWN_MIN:
        return RULE_FIRE_COOLDOWN_MIN
    if v > RULE_FIRE_COOLDOWN_MAX:
        return RULE_FIRE_COOLDOWN_MAX
    return v


def llm_reentry_seconds() -> int:
    """
    Extra quiet window after a pure-LLM fire (covers SL-then-reenter spray).

    Default 3600s. Clamp 0–7200; 0 disables the extra (rule 900s still applies).
    """
    return clamp_rule_fire_cooldown_seconds(
        _env_int("KEEL_LLM_REENTRY_SECONDS", LLM_REENTRY_DEFAULT)
    )


def is_cooldown_fire(
    action: Any,
    diag: dict[str, Any] | None,
    *,
    policy_name: str | None = None,
) -> bool:
    """True when this row should start / consume the per-instrument fire cooldown."""
    act = str(action or "").upper().strip()
    if act not in _FIRE_ACTIONS:
        return False
    pol = str(policy_name or "").strip().lower()
    if pol in LLM_COOLDOWN_POLICIES:
        return True
    return is_full_gate_fire(action, diag, policy_name=policy_name)


def full_gate_fire_recent(
    ledger: Any,
    *,
    inst_id: str,
    cooldown_seconds: float,
    now: float | None = None,
) -> bool:
    """True when a full-gate fire for ``inst_id`` is inside the cooldown window."""
    if cooldown_seconds <= 0:
        return False
    if ledger is None:
        return False
    iid = str(inst_id or "").strip()
    if not iid:
        return False
    stamp = float(now if now is not None else time.time())
    cutoff = stamp - float(cooldown_seconds)
    getter = getattr(ledger, "get_decisions", None)
    if not callable(getter):
        return False
    try:
        rows = getter(since=cutoff, inst_id=iid, limit=50)
    except Exception:
        return False
    for row in rows or []:
        ts = getattr(row, "timestamp", None)
        if ts is None and isinstance(row, dict):
            ts = row.get("timestamp")
        try:
            if ts is None or float(ts) < cutoff:
                continue
        except (TypeError, ValueError):
            continue
        if isinstance(row, dict):
            action = row.get("action")
            pol = row.get("policy_name") or ""
            calc = row.get("calculus_data")
        else:
            action = getattr(row, "action", None)
            pol = getattr(row, "policy_name", None) or ""
            calc = getattr(row, "calculus_data", None)
        diag = signal_diag_from_calculus(calc)
        if is_cooldown_fire(action, diag, policy_name=str(pol)):
            return True
    return False


def apply_rule_fire_cooldown(
    decision: Decision,
    *,
    ledger: Any,
    cooldown_seconds: int | float,
    now: float | None = None,
    policy_name: str = "rule",
) -> Decision:
    """
    If ``decision`` is a full-gate fire and one already fired recently for the
    same instrument, rewrite to WAIT with cooldown flags on signal_diag.

    Otherwise return ``decision`` unchanged (optionally annotated inactive).
    """
    cd = clamp_rule_fire_cooldown_seconds(cooldown_seconds)
    if str(policy_name or "").strip().lower() in LLM_COOLDOWN_POLICIES:
        extra = llm_reentry_seconds()
        if extra > 0:
            cd = max(cd, extra)
    if cd <= 0:
        return decision
    if not is_cooldown_fire(
        decision.action,
        decision.signal_diag,
        policy_name=policy_name,
    ):
        return decision
    stamp = float(now if now is not None else time.time())
    if not full_gate_fire_recent(
        ledger,
        inst_id=decision.inst_id,
        cooldown_seconds=cd,
        now=stamp,
    ):
        return decision

    diag = neutralize_suppressed_fire_diag(
        decision.signal_diag,
        gate=SUPPRESS_COOLDOWN_GATE,
        extra={
            "fire_cooldown_active": True,
            "fire_cooldown_seconds": int(cd),
        },
    )
    return Decision(
        inst_id=decision.inst_id,
        action="WAIT",
        confidence=decision.confidence,
        reason=f"fire cooldown ({int(cd)}s) — suppress spray",
        signal_diag=diag,
    )


__all__ = [
    "RULE_FIRE_COOLDOWN_DEFAULT",
    "RULE_FIRE_COOLDOWN_MAX",
    "RULE_FIRE_COOLDOWN_MIN",
    "apply_rule_fire_cooldown",
    "clamp_rule_fire_cooldown_seconds",
    "full_gate_fire_recent",
    "is_cooldown_fire",
    "llm_reentry_seconds",
    "LLM_COOLDOWN_POLICIES",
    "LLM_REENTRY_DEFAULT",
]
