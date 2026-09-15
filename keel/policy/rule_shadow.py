"""
Parallel rule shadow for LLM-as-trader cycles.

When primary policy is ``llm`` (or ``KEEL_RULE_SHADOW=1``), also run
``RuleDecisionPolicy`` on the same ``PolicyContext`` and attach a
``rule_shadow`` object under ``calculus_data`` for daily LLM-vs-rule review.
The shadow is never executed.
"""
from __future__ import annotations

import os
from typing import Any

from keel.domain.decision import Decision
from keel.policy.stub import resolve_rule_variant

_FIRE_ACTIONS = frozenset({"BUY_LONG", "SELL_SHORT"})
_KNOWN_ACTIONS = _FIRE_ACTIONS | {"WAIT"}


def _env_bool(key: str, default: bool) -> bool:
    raw = (os.environ.get(key) or "").strip().lower()
    if not raw:
        return default
    if raw in ("1", "true", "yes", "on"):
        return True
    if raw in ("0", "false", "no", "off"):
        return False
    return default


def rule_shadow_enabled(policy_name: str) -> bool:
    """
    True when rule shadow should run.

    Default **on** whenever primary policy is ``llm``; set
    ``KEEL_RULE_SHADOW=0`` to disable. Explicit ``KEEL_RULE_SHADOW=1`` also
    enables shadow for non-llm primaries (audit only).
    """
    primary = str(policy_name or "").strip().lower()
    default_on = primary == "llm"
    return _env_bool("KEEL_RULE_SHADOW", default_on)


def actions_agree(primary_action: str, rule_action: str) -> bool | None:
    """
    Compare primary vs rule actions.

    - ``True`` if both WAIT, or both same fire side (BUY_LONG / SELL_SHORT)
    - ``False`` if they diverge (one WAIT one fire, or opposite sides)
    - ``None`` if either action is unknown / unclear
    """
    p = str(primary_action or "").strip().upper()
    r = str(rule_action or "").strip().upper()
    if p not in _KNOWN_ACTIONS or r not in _KNOWN_ACTIONS:
        return None
    if p == "WAIT" and r == "WAIT":
        return True
    if p in _FIRE_ACTIONS and r in _FIRE_ACTIONS:
        return p == r
    return False


def build_rule_shadow(
    rule: Decision,
    *,
    primary_action: str,
    executed_policy: str = "llm",
    rule_variant: str | None = None,
) -> dict[str, Any]:
    """Build the ``calculus_data.rule_shadow`` payload for one instrument."""
    variant = rule_variant
    if not variant:
        diag = rule.signal_diag if isinstance(rule.signal_diag, dict) else None
        if isinstance(diag, dict) and diag.get("rule_variant"):
            variant = str(diag["rule_variant"])
        else:
            variant = resolve_rule_variant()

    agree = actions_agree(primary_action, str(rule.action))
    payload: dict[str, Any] = {
        "action": str(rule.action),
        "reason": str(rule.reason or ""),
        "confidence": float(rule.confidence or 0.0),
        "margin_usdt": float(rule.margin_usdt or 0.0),
        "entry_price": rule.entry_price,
        "take_profit": rule.take_profit,
        "stop_loss": rule.stop_loss,
        "rule_variant": str(variant or ""),
        "agree": agree,
        "executed_policy": str(executed_policy or "llm"),
    }
    if isinstance(rule.signal_diag, dict) and rule.signal_diag:
        payload["signal_diag"] = dict(rule.signal_diag)
    return payload


def collect_rule_shadows(
    *,
    primary_decisions: dict[str, Decision],
    rule_decisions: dict[str, Decision],
    instrument_ids: list[str],
    executed_policy: str = "llm",
) -> dict[str, dict[str, Any]]:
    """Map inst_id → rule_shadow payload for instruments present in both maps."""
    out: dict[str, dict[str, Any]] = {}
    for inst_id in instrument_ids:
        rule_d = rule_decisions.get(inst_id)
        primary_d = primary_decisions.get(inst_id)
        if rule_d is None:
            continue
        primary_action = str(primary_d.action) if primary_d is not None else ""
        out[inst_id] = build_rule_shadow(
            rule_d,
            primary_action=primary_action,
            executed_policy=executed_policy,
        )
    return out
