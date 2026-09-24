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

Opt A — post-exit cooldown (LLM): after a realized close (TP/SL/manual),
also suppress re-entry for ``KEEL_LLM_POST_EXIT_COOLDOWN_SECONDS``
(default 7200; llm_demo profile injects the same). Fire→fire reentry alone
misses TP-then-reopen when the original fire aged out.
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
LLM_POST_EXIT_COOLDOWN_DEFAULT = 7200
LLM_POST_EXIT_COOLDOWN_MIN = 0
LLM_POST_EXIT_COOLDOWN_MAX = 86400
_FIRE_ACTIONS = frozenset({"BUY_LONG", "SELL_SHORT"})
# Pure LLM has no signal_diag.missing=[]; still spray-guard like a full-gate.
LLM_COOLDOWN_POLICIES = frozenset({"llm"})
_CLOSE_EVENT_TYPES = frozenset({"sl_hit", "tp_hit", "close"})


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


def clamp_llm_post_exit_cooldown_seconds(raw: int | float | None) -> int:
    """Clamp KEEL_LLM_POST_EXIT_COOLDOWN_SECONDS into [0, 86400]; 0 disables."""
    try:
        v = int(raw) if raw is not None else LLM_POST_EXIT_COOLDOWN_DEFAULT
    except (TypeError, ValueError):
        v = LLM_POST_EXIT_COOLDOWN_DEFAULT
    if v < LLM_POST_EXIT_COOLDOWN_MIN:
        return LLM_POST_EXIT_COOLDOWN_MIN
    if v > LLM_POST_EXIT_COOLDOWN_MAX:
        return LLM_POST_EXIT_COOLDOWN_MAX
    return v


def llm_reentry_seconds() -> int:
    """
    Extra quiet window after a pure-LLM fire (covers SL-then-reenter spray).

    Default 3600s. Clamp 0–7200; 0 disables the extra (rule 900s still applies).
    """
    return clamp_rule_fire_cooldown_seconds(
        _env_int("KEEL_LLM_REENTRY_SECONDS", LLM_REENTRY_DEFAULT)
    )


def llm_post_exit_cooldown_seconds() -> int:
    """
    Quiet window after a realized close before same-inst LLM re-entry.

    Default 7200s (also injected by llm_demo profile). Clamp 0–86400; 0 off.
    """
    return clamp_llm_post_exit_cooldown_seconds(
        _env_int("KEEL_LLM_POST_EXIT_COOLDOWN_SECONDS", LLM_POST_EXIT_COOLDOWN_DEFAULT)
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


def latest_close_timestamp(
    ledger: Any,
    *,
    inst_id: str,
) -> float | None:
    """
    Latest realized-close timestamp for ``inst_id``.

    Prefers ``trades.action=close``; falls back to ``sl_hit`` / ``tp_hit`` /
    ``close`` events when trades are missing.
    """
    if ledger is None:
        return None
    iid = str(inst_id or "").strip()
    if not iid:
        return None
    best: float | None = None

    getter = getattr(ledger, "get_trades", None)
    if callable(getter):
        try:
            rows = getter(inst_id=iid, action="close", limit=1)
        except Exception:
            rows = None
        for row in rows or []:
            ts = getattr(row, "timestamp", None)
            if ts is None and isinstance(row, dict):
                ts = row.get("timestamp")
            try:
                if ts is not None:
                    best = float(ts)
                    break
            except (TypeError, ValueError):
                continue

    events = getattr(ledger, "get_events", None)
    if callable(events):
        for et in ("tp_hit", "sl_hit", "close"):
            try:
                ev_rows = events(event_type=et, inst_id=iid, limit=1)
            except Exception:
                continue
            for row in ev_rows or []:
                ts = getattr(row, "timestamp", None)
                if ts is None and isinstance(row, dict):
                    ts = row.get("timestamp")
                try:
                    if ts is None:
                        continue
                    tsf = float(ts)
                except (TypeError, ValueError):
                    continue
                if best is None or tsf > best:
                    best = tsf
                break
    return best


def post_exit_cooldown_active(
    ledger: Any,
    *,
    inst_id: str,
    cooldown_seconds: float,
    now: float | None = None,
) -> bool:
    """True when latest close for ``inst_id`` is inside the post-exit window."""
    if cooldown_seconds <= 0:
        return False
    stamp = float(now if now is not None else time.time())
    ts = latest_close_timestamp(ledger, inst_id=inst_id)
    if ts is None:
        return False
    return (stamp - float(ts)) < float(cooldown_seconds)


def _suppress_cooldown(
    decision: Decision,
    *,
    seconds: int,
    reason_code: str,
    reason_text: str,
) -> Decision:
    diag = neutralize_suppressed_fire_diag(
        decision.signal_diag,
        gate=SUPPRESS_COOLDOWN_GATE,
        extra={
            "fire_cooldown_active": True,
            "fire_cooldown_ok": False,
            "fire_cooldown_seconds": int(seconds),
            "fire_cooldown_reason": reason_code,
        },
    )
    return Decision(
        inst_id=decision.inst_id,
        action="WAIT",
        confidence=decision.confidence,
        reason=reason_text,
        signal_diag=diag,
    )


def apply_rule_fire_cooldown(
    decision: Decision,
    *,
    ledger: Any,
    cooldown_seconds: int | float,
    now: float | None = None,
    policy_name: str = "rule",
) -> Decision:
    """
    If ``decision`` is a cooldown-eligible fire and one already fired recently
    for the same instrument (or, for LLM, a close is inside the post-exit
    window), rewrite to WAIT with cooldown flags on signal_diag.

    Otherwise return ``decision`` unchanged.
    """
    pol = str(policy_name or "").strip().lower()
    stamp = float(now if now is not None else time.time())

    if not is_cooldown_fire(
        decision.action,
        decision.signal_diag,
        policy_name=policy_name,
    ):
        return decision

    # Opt A: LLM post-exit quiet window from latest realized close.
    if pol in LLM_COOLDOWN_POLICIES:
        post_cd = llm_post_exit_cooldown_seconds()
        if post_cd > 0 and post_exit_cooldown_active(
            ledger,
            inst_id=decision.inst_id,
            cooldown_seconds=post_cd,
            now=stamp,
        ):
            return _suppress_cooldown(
                decision,
                seconds=post_cd,
                reason_code="post_exit_cooldown",
                reason_text=(
                    f"post-exit cooldown ({int(post_cd)}s) — "
                    "suppress re-entry after close"
                ),
            )

    cd = clamp_rule_fire_cooldown_seconds(cooldown_seconds)
    if pol in LLM_COOLDOWN_POLICIES:
        extra = llm_reentry_seconds()
        if extra > 0:
            cd = max(cd, extra)
    if cd <= 0:
        return decision
    if not full_gate_fire_recent(
        ledger,
        inst_id=decision.inst_id,
        cooldown_seconds=cd,
        now=stamp,
    ):
        return decision

    return _suppress_cooldown(
        decision,
        seconds=int(cd),
        reason_code="fire_reentry",
        reason_text=f"fire cooldown ({int(cd)}s) — suppress spray",
    )


__all__ = [
    "RULE_FIRE_COOLDOWN_DEFAULT",
    "RULE_FIRE_COOLDOWN_MAX",
    "RULE_FIRE_COOLDOWN_MIN",
    "LLM_POST_EXIT_COOLDOWN_DEFAULT",
    "LLM_POST_EXIT_COOLDOWN_MAX",
    "LLM_POST_EXIT_COOLDOWN_MIN",
    "apply_rule_fire_cooldown",
    "clamp_llm_post_exit_cooldown_seconds",
    "clamp_rule_fire_cooldown_seconds",
    "full_gate_fire_recent",
    "is_cooldown_fire",
    "latest_close_timestamp",
    "llm_post_exit_cooldown_seconds",
    "llm_reentry_seconds",
    "post_exit_cooldown_active",
    "LLM_COOLDOWN_POLICIES",
    "LLM_REENTRY_DEFAULT",
]
