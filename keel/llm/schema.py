"""
Decision JSON schema helpers shared by LLM client and policy tests.

``DECISION_SCHEMA`` lives here for import clarity; ``keel.llm.client`` keeps a
compatible re-export so existing callers do not break.
"""
from __future__ import annotations

from typing import Any

from keel.domain.decision import Decision
from keel.llm.client import DECISION_SCHEMA


def validate_decision_payload(
    data: Any,
    *,
    instrument_ids: list[str] | None = None,
) -> dict[str, Any]:
    """
    Lightweight structural validation of an LLM decision JSON object.

    Does not pull in jsonschema; checks the fields required by DECISION_SCHEMA
    and returns ``{valid, errors, decisions_count}``.
    """
    errors: list[str] = []
    if not isinstance(data, dict):
        return {"valid": False, "errors": ["payload must be an object"], "decisions_count": 0}

    if "decisions" not in data:
        errors.append("missing required field: decisions")
    decisions = data.get("decisions")
    if decisions is not None and not isinstance(decisions, dict):
        errors.append("decisions must be an object")
        decisions = {}
    decisions = decisions or {}

    allowed_actions = set(DECISION_SCHEMA["properties"]["decisions"]["additionalProperties"]["properties"]["action"]["enum"])

    ids = instrument_ids or list(decisions.keys())
    for inst_id in ids:
        raw = decisions.get(inst_id)
        if raw is None:
            errors.append(f"missing decision for {inst_id}")
            continue
        if not isinstance(raw, dict):
            errors.append(f"{inst_id}: decision must be an object")
            continue
        if "action" not in raw:
            errors.append(f"{inst_id}: missing required field action")
            continue
        action = str(raw.get("action", "")).upper()
        if action not in allowed_actions:
            errors.append(f"{inst_id}: invalid action {raw.get('action')!r}")
        conf = raw.get("confidence")
        if conf is not None:
            try:
                c = float(conf)
                if c < 0 or c > 100:
                    errors.append(f"{inst_id}: confidence out of range 0-100")
            except (TypeError, ValueError):
                errors.append(f"{inst_id}: confidence must be a number")
        lev = raw.get("leverage")
        if lev is not None:
            try:
                li = int(lev)
                if li < 1 or li > 10:
                    errors.append(f"{inst_id}: leverage out of range 1-10")
            except (TypeError, ValueError):
                errors.append(f"{inst_id}: leverage must be an integer")

    return {
        "valid": not errors,
        "errors": errors,
        "decisions_count": len(decisions),
        "schema_title": "keel.decision.v1",
    }


def decision_to_payload(decision: Decision) -> dict[str, Any]:
    """Serialize a Decision to the wire JSON shape used by DECISION_SCHEMA."""
    return {
        "action": decision.action,
        "confidence": decision.confidence,
        "leverage": decision.leverage,
        "margin_usdt": decision.margin_usdt,
        "entry_price": decision.entry_price,
        "take_profit_price": decision.take_profit,
        "stop_loss_price": decision.stop_loss,
        "summary_reason": decision.reason,
    }
