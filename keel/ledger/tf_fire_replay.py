"""
E2C: offline TF (+ optional MR) full-gate fire-rate replay on ledger snapshots.

Replays ``rule_based_decision`` / ``diagnose_rule_signal`` under a forced
``KEEL_RULE_VARIANT`` (in-process env only — never writes ``.env``).

Markout: not computed here. Live full-gate fee-aware markout remains
``scripts/full_gate_markout.py`` / ``keel.ledger.full_gate.compute_full_gate_markout``
(those operate on decisions that already fired live). Counterfactual markout
for replay-only fires is out of scope for this helper.
"""
from __future__ import annotations

import os
from collections import Counter
from contextlib import contextmanager
from typing import Any, Iterator

from keel.domain.decision import validate_decision
from keel.ledger.decision_export import (
    action_histogram,
    replay_rule_on_rows,
    snapshot_from_export_row,
)
from keel.policy.stub import diagnose_rule_signal, rule_based_decision

_VARIANT_ENV = "KEEL_RULE_VARIANT"
_FIRE_ACTIONS = frozenset({"BUY_LONG", "SELL_SHORT"})


def normalize_variant(raw: str | None) -> str:
    """Map CLI / env aliases to ``trend_follow`` or ``mean_revert``."""
    v = (raw or "trend_follow").strip().lower()
    if v in ("trend_follow", "trend-follow", "tf"):
        return "trend_follow"
    if v in ("mean_revert", "mean-revert", "mr"):
        return "mean_revert"
    return "trend_follow"


@contextmanager
def forced_rule_variant(variant: str) -> Iterator[str]:
    """
    Temporarily set ``KEEL_RULE_VARIANT`` in-process; restore on exit.

    Does not touch dotenv / ``.env`` files.
    """
    normalized = normalize_variant(variant)
    prev = os.environ.get(_VARIANT_ENV)
    os.environ[_VARIANT_ENV] = normalized
    try:
        yield normalized
    finally:
        if prev is None:
            os.environ.pop(_VARIANT_ENV, None)
        else:
            os.environ[_VARIANT_ENV] = prev


def _is_full_gate(action: str | None, diag: dict[str, Any] | None) -> bool:
    if str(action or "").upper() not in _FIRE_ACTIONS:
        return False
    if not isinstance(diag, dict):
        return False
    missing = diag.get("missing")
    return isinstance(missing, list) and len(missing) == 0


def summarize_replay(
    results: list[dict[str, Any]],
    *,
    variant: str,
) -> dict[str, Any]:
    """
    Aggregate fire-rate / missing-gate stats from ``replay_rule_on_rows`` output.

    Returns counts by instrument + action, top missing gates, and 1-missing
    near-fire counts (WAIT with exactly one missing gate, nearest long/short).
    """
    considered = [r for r in results if not r.get("skipped")]
    skipped = sum(1 for r in results if r.get("skipped"))
    n = len(considered)

    by_inst: dict[str, dict[str, Any]] = {}
    by_action: Counter[str] = Counter()
    missing_hist: Counter[str] = Counter()
    missing_combo: Counter[str] = Counter()
    full_gate = 0
    near_1 = 0
    soft_tf = 0
    macd_lag_ok = 0
    has_trend_1h = 0

    for row in considered:
        inst = str(row.get("inst_id") or "UNKNOWN")
        action = str(row.get("action") or "UNKNOWN").upper()
        diag = row.get("signal_diag") if isinstance(row.get("signal_diag"), dict) else {}
        by_action[action] += 1

        entry = by_inst.setdefault(
            inst,
            {
                "n": 0,
                "full_gate": 0,
                "by_action": Counter(),
                "near_1_missing": 0,
            },
        )
        entry["n"] += 1
        entry["by_action"][action] += 1

        t1h = str(diag.get("trend_1h") or "").lower()
        if t1h in ("bullish", "bearish", "neutral"):
            # Always present after diagnose; count non-default multi-TF only when
            # require_1h / TF path actually used stored 1h (audit: not unknown).
            has_trend_1h += 1

        if diag.get("volume_path") == "soft_tf":
            soft_tf += 1
        if diag.get("macd_lag_ok"):
            macd_lag_ok += 1

        missing = diag.get("missing")
        if not isinstance(missing, list):
            missing = []

        if _is_full_gate(action, diag):
            full_gate += 1
            entry["full_gate"] += 1
        else:
            for g in missing:
                missing_hist[str(g)] += 1
            if missing:
                missing_combo[",".join(str(g) for g in missing)] += 1

        if (
            action == "WAIT"
            and len(missing) == 1
            and str(diag.get("nearest") or "").lower() in ("long", "short")
        ):
            near_1 += 1
            entry["near_1_missing"] += 1

    by_inst_out: dict[str, Any] = {}
    for inst, info in sorted(by_inst.items()):
        ni = int(info["n"])
        fg = int(info["full_gate"])
        by_inst_out[inst] = {
            "n": ni,
            "full_gate": fg,
            "full_gate_rate": (fg / ni) if ni else 0.0,
            "by_action": dict(info["by_action"]),
            "near_1_missing": int(info["near_1_missing"]),
        }

    return {
        "variant": normalize_variant(variant),
        "n_snapshots": n,
        "skipped_incomplete": skipped,
        "full_gate_count": full_gate,
        "full_gate_rate": (full_gate / n) if n else 0.0,
        "by_action": dict(by_action),
        "by_instrument": by_inst_out,
        "top_missing_gates": dict(missing_hist.most_common(15)),
        "top_missing_combos": dict(missing_combo.most_common(10)),
        "near_1_missing_count": near_1,
        "near_1_missing_rate": (near_1 / n) if n else 0.0,
        "soft_tf_count": soft_tf,
        "macd_lag_ok_count": macd_lag_ok,
        "diag_rows_with_trend_1h_field": has_trend_1h,
        "actions": action_histogram(results),
        "markout": {
            "skipped": True,
            "reason": (
                "Replay fires are counterfactual; use scripts/full_gate_markout.py "
                "for live full-gate decisions. Fee-aware markout of replay-only "
                "hits is out of scope for E2C."
            ),
        },
    }


def replay_under_variant(
    rows: list[dict[str, Any]],
    variant: str,
) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    """Force variant, replay rule on export rows, return (results, summary)."""
    with forced_rule_variant(variant) as v:
        results, _replayed, _skipped = replay_rule_on_rows(rows)
        summary = summarize_replay(results, variant=v)
    return results, summary


def diagnose_crafted(snapshot: Any, variant: str = "trend_follow") -> dict[str, Any]:
    """Unit-test helper: diagnose + decide one crafted MarketSnapshot under variant."""
    with forced_rule_variant(variant) as v:
        diag = diagnose_rule_signal(snapshot)
        decision = validate_decision(rule_based_decision(snapshot))
        return {
            "variant": v,
            "action": decision.action,
            "signal_diag": decision.signal_diag or diag,
            "full_gate": _is_full_gate(decision.action, decision.signal_diag or diag),
        }


def rows_from_factor_snapshots(
    snaps: list[Any],
) -> list[dict[str, Any]]:
    """
    Build export-shaped rows from FactorSnapshot objects (no decision join).

    Useful when replaying factors that lack a matched decision calculus.
    """
    from keel.ledger.decision_export import (
        build_export_row,
        factor_dict_from_snapshot,
    )

    out: list[dict[str, Any]] = []
    for snap in snaps:
        factors = factor_dict_from_snapshot(snap)
        out.append(
            build_export_row(
                decision_id=getattr(snap, "id", None),
                timestamp=float(getattr(snap, "timestamp", 0) or 0),
                inst_id=str(getattr(snap, "inst_id", "") or ""),
                action="WAIT",
                policy_name="rule",
                calculus_data=None,
                entry_price=float(getattr(snap, "price", 0) or 0) or None,
                factors=factors,
            )
        )
    return out


def load_replay_rows(
    ledger: Any,
    *,
    hours: float = 168.0,
    market_source: str = "any",
    limit: int = 50000,
    prefer: str = "decisions",
) -> list[dict[str, Any]]:
    """
    Load replay rows from local SQLite ledger.

    ``prefer=decisions`` (default): ``export_decisions`` with factor join +
    calculus signal_diag (best for trend_1h / volume_percentile).
    ``prefer=factors``: recent factor_snapshots only (no decision calculus).
    """
    hours_f = max(0.0, float(hours))
    if prefer == "factors":
        # get_factor_snapshots is limit-only; pull a wide limit then filter by time.
        import time as _time

        since = _time.time() - hours_f * 3600.0
        snaps = ledger.get_factor_snapshots(inst_id=None, limit=max(1, int(limit)))
        snaps = [s for s in snaps if float(getattr(s, "timestamp", 0) or 0) >= since]
        return rows_from_factor_snapshots(snaps)

    return ledger.export_decisions(
        hours=hours_f,
        market_source=market_source,
        limit=max(1, int(limit)),
        include_factors=True,
    )


# Re-export for callers/tests
__all__ = [
    "diagnose_crafted",
    "forced_rule_variant",
    "load_replay_rows",
    "normalize_variant",
    "replay_under_variant",
    "rows_from_factor_snapshots",
    "snapshot_from_export_row",
    "summarize_replay",
]
