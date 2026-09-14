"""
Execution / ledger provenance stamps for historical traceability.

Links decision → shadow/fill/trade/event rows via decision_id and
market_source / rule_variant so Monitor history can show完整溯源.
"""
from __future__ import annotations

from typing import Any

from keel.domain.decision import Decision


def rule_variant_of(decision: Decision | None) -> str | None:
    if decision is None:
        return None
    diag = decision.signal_diag if isinstance(decision.signal_diag, dict) else None
    if not diag:
        return None
    raw = diag.get("rule_variant")
    if raw is None or raw == "":
        return None
    return str(raw)


def market_source_of(decision: Decision | None) -> str | None:
    if decision is None:
        return None
    raw = getattr(decision, "market_source", None)
    if raw is None or raw == "":
        return None
    return str(raw)


def ledger_id_of(decision: Decision | None) -> int | None:
    if decision is None:
        return None
    raw = getattr(decision, "ledger_id", None)
    if raw is None:
        return None
    try:
        return int(raw)
    except (TypeError, ValueError):
        return None


def provenance_fields(
    decision: Decision | None,
    *,
    policy_name: str | None = None,
) -> dict[str, Any]:
    """
    Compact provenance dict for trade.metadata / event.data.

    Only includes keys that are present so older consumers stay soft-fail.
    """
    out: dict[str, Any] = {}
    did = ledger_id_of(decision)
    if did is not None and did > 0:
        out["decision_id"] = did
    ms = market_source_of(decision)
    if ms:
        out["market_source"] = ms
    rv = rule_variant_of(decision)
    if rv:
        out["rule_variant"] = rv
    if policy_name:
        out["policy_name"] = str(policy_name)
    return out


def meta_get(meta: dict[str, Any] | None, *keys: str) -> Any:
    """First present key from trade/event metadata."""
    if not isinstance(meta, dict):
        return None
    for key in keys:
        if key in meta and meta[key] is not None and meta[key] != "":
            return meta[key]
    return None


def coerce_optional_int(raw: Any) -> int | None:
    if raw is None or raw == "":
        return None
    try:
        return int(raw)
    except (TypeError, ValueError):
        return None


def coerce_optional_bool(raw: Any) -> bool | None:
    if raw is None or raw == "":
        return None
    if isinstance(raw, bool):
        return raw
    if isinstance(raw, (int, float)):
        return bool(raw)
    s = str(raw).strip().lower()
    if s in ("1", "true", "yes", "on"):
        return True
    if s in ("0", "false", "no", "off"):
        return False
    return None
