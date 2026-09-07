"""
Q2.1: export ledger decisions for offline rule-param replay/compare.

Prefer replaying ``rule_based_decision`` on stored factor / calculus inputs
(okx_public cohort) over inventing new market data.
"""
from __future__ import annotations

import json
from typing import Any

from keel.domain.decision import validate_decision
from keel.factors.market_data import MarketSnapshot
from keel.policy.stub import rule_based_decision

# Factor keys needed to reconstruct a replayable MarketSnapshot.
# R5+/E2A: trend_1h/trend_4h + volume_percentile matter for TF hard 1h + soft volume.
_FACTOR_KEYS = (
    "price",
    "rsi_14",
    "ema_9",
    "ema_21",
    "atr_14",
    "macd_histogram",
    "trend_15m",
    "trend_1h",
    "trend_4h",
    "volume_ratio",
    "volume_percentile",
)

# Minimum numeric factors for gate replay (price/atr may be filled from fallbacks).
_GATE_KEYS = (
    "rsi_14",
    "ema_9",
    "ema_21",
    "macd_histogram",
    "trend_15m",
    "volume_ratio",
)


def market_source_of(calculus: dict[str, Any] | None) -> str | None:
    if not isinstance(calculus, dict):
        return None
    raw = calculus.get("market_source")
    if raw is None or raw == "":
        return None
    return str(raw)


def signal_diag_of(calculus: dict[str, Any] | None) -> dict[str, Any] | None:
    if not isinstance(calculus, dict):
        return None
    raw = calculus.get("signal_diag")
    return raw if isinstance(raw, dict) else None


def factor_dict_from_snapshot(snap: Any) -> dict[str, Any]:
    """Build a plain factors dict from a FactorSnapshot (or duck-typed object)."""
    payload = getattr(snap, "payload", None) or {}
    if not isinstance(payload, dict):
        payload = {}
    data_valid = payload.get("data_valid")
    if data_valid is None:
        data_valid = bool(getattr(snap, "price", 0) > 0 and getattr(snap, "atr_14", 0) > 0)
    trend_1h = payload.get("trend_1h")
    if trend_1h is None or trend_1h == "":
        trend_1h = "neutral"
    trend_4h = payload.get("trend_4h")
    if trend_4h is None or trend_4h == "":
        trend_4h = "neutral"
    vol_pct = payload.get("volume_percentile")
    try:
        vol_pct_out: float | None = float(vol_pct) if vol_pct is not None else None
    except (TypeError, ValueError):
        vol_pct_out = None
    return {
        "price": float(getattr(snap, "price", 0) or 0),
        "rsi_14": float(getattr(snap, "rsi_14", 0) or 0),
        "ema_9": float(getattr(snap, "ema_9", 0) or 0),
        "ema_21": float(getattr(snap, "ema_21", 0) or 0),
        "atr_14": float(getattr(snap, "atr_14", 0) or 0),
        "macd_histogram": float(getattr(snap, "macd_histogram", 0) or 0),
        "trend_15m": str(getattr(snap, "trend_15m", "neutral") or "neutral"),
        "trend_1h": str(trend_1h),
        "trend_4h": str(trend_4h),
        "volume_ratio": float(getattr(snap, "volume_ratio", 1) or 1),
        "volume_percentile": vol_pct_out,
        "data_valid": bool(data_valid),
        "data_quality_reason": str(payload.get("data_quality_reason") or ""),
    }


def _merge_factor_sources(
    *,
    factors: dict[str, Any] | None,
    calculus: dict[str, Any] | None,
    entry_price: float | None,
) -> dict[str, Any]:
    """
    Merge factor_snapshot + calculus/signal_diag into one factor bag.

    Preference: explicit factors (ledger join) > signal_diag numerics >
    top-level calculus rsi/trend > entry_price for price fallback.
    """
    out: dict[str, Any] = {}
    if isinstance(factors, dict):
        out.update(factors)

    calc = calculus if isinstance(calculus, dict) else {}
    diag = signal_diag_of(calc) or {}

    for key in _FACTOR_KEYS:
        if out.get(key) is not None and out.get(key) != "":
            continue
        if key in diag and diag[key] is not None:
            out[key] = diag[key]
        elif key in calc and calc[key] is not None:
            out[key] = calc[key]

    if (out.get("price") is None or float(out.get("price") or 0) <= 0) and entry_price:
        out["price"] = float(entry_price)

    if "data_valid" not in out:
        if "data_valid" in diag:
            out["data_valid"] = bool(diag["data_valid"])
        elif float(out.get("price") or 0) > 0 and float(out.get("atr_14") or 0) > 0:
            out["data_valid"] = True
        else:
            out["data_valid"] = False

    return out


def build_export_row(
    *,
    decision_id: int | None,
    timestamp: float,
    inst_id: str,
    action: str,
    policy_name: str = "",
    calculus_data: dict[str, Any] | None = None,
    entry_price: float | None = None,
    factors: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Normalize one decision into an export / replay record."""
    calc = dict(calculus_data) if isinstance(calculus_data, dict) else None
    diag = signal_diag_of(calc)
    merged = _merge_factor_sources(
        factors=factors, calculus=calc, entry_price=entry_price
    )
    replayable = is_replayable_factors(merged)
    return {
        "id": decision_id,
        "instrument": inst_id,
        "inst_id": inst_id,
        "action": action,
        "timestamp": float(timestamp),
        "policy_name": policy_name or "",
        "market_source": market_source_of(calc),
        "signal_diag": diag,
        "calculus_data": calc,
        "factors": merged if merged else None,
        "entry_price": entry_price,
        "replayable": replayable,
    }


def is_replayable_factors(factors: dict[str, Any] | None) -> bool:
    """True when gate inputs are present (price/atr may use soft fallbacks)."""
    if not isinstance(factors, dict):
        return False
    for key in _GATE_KEYS:
        if factors.get(key) is None:
            return False
    # trend must be a non-empty string
    if not str(factors.get("trend_15m") or "").strip():
        return False
    try:
        for key in ("rsi_14", "ema_9", "ema_21", "macd_histogram", "volume_ratio"):
            float(factors[key])
    except (TypeError, ValueError):
        return False
    return True


def snapshot_from_export_row(row: dict[str, Any]) -> MarketSnapshot | None:
    """
    Reconstruct MarketSnapshot for offline rule replay.

    Limitation: if calculus lacks signal_diag / factors and no factor_snapshot
    join exists, returns None (caller should skip and count).
    When gate factors exist but price/atr are missing, uses soft fallbacks
    (entry_price or 100.0 / atr 1.0) so action classification still works —
    geometry is approximate; histograms remain meaningful for threshold A/B.
    """
    factors = row.get("factors")
    if not isinstance(factors, dict):
        factors = _merge_factor_sources(
            factors=None,
            calculus=row.get("calculus_data")
            if isinstance(row.get("calculus_data"), dict)
            else None,
            entry_price=row.get("entry_price"),
        )
    if not is_replayable_factors(factors):
        return None

    price = float(factors.get("price") or 0)
    atr = float(factors.get("atr_14") or 0)
    data_valid = bool(factors.get("data_valid", True))

    if price <= 0:
        ep = row.get("entry_price")
        price = float(ep) if ep else 100.0
    if atr <= 0:
        # Soft fallback: enough for data_valid + RR geometry; gates ignore atr.
        atr = 1.0
        if not factors.get("atr_14"):
            # Keep data_valid from diag when price was also fallback-only.
            data_valid = bool(factors.get("data_valid", True))

    trend = str(factors.get("trend_15m") or "neutral")
    if trend not in ("bullish", "bearish", "neutral"):
        trend = "neutral"
    trend_1h = str(factors.get("trend_1h") or "neutral")
    if trend_1h not in ("bullish", "bearish", "neutral"):
        trend_1h = "neutral"
    trend_4h = str(factors.get("trend_4h") or "neutral")
    if trend_4h not in ("bullish", "bearish", "neutral"):
        trend_4h = "neutral"
    vol_pct_raw = factors.get("volume_percentile")
    try:
        vol_pct: float | None = float(vol_pct_raw) if vol_pct_raw is not None else None
    except (TypeError, ValueError):
        vol_pct = None

    inst = str(row.get("inst_id") or row.get("instrument") or "")
    ts = float(row.get("timestamp") or 0)
    return MarketSnapshot(
        inst_id=inst,
        name=inst,
        timestamp=ts,
        price=price,
        atr_14=atr,
        rsi_14=float(factors["rsi_14"]),
        ema_9=float(factors["ema_9"]),
        ema_21=float(factors["ema_21"]),
        macd_histogram=float(factors["macd_histogram"]),
        volume_ratio=float(factors["volume_ratio"]),
        volume_percentile=vol_pct,
        trend_15m=trend,  # type: ignore[arg-type]
        trend_1h=trend_1h,  # type: ignore[assignment]
        trend_4h=trend_4h,  # type: ignore[assignment]
        data_valid=data_valid,
        data_quality_reason=str(factors.get("data_quality_reason") or "ledger_replay"),
    )


def load_export_path(path: str | Any) -> list[dict[str, Any]]:
    """Load JSONL or JSON array export produced by export_decisions."""
    from pathlib import Path

    p = Path(path)
    text = p.read_text(encoding="utf-8").strip()
    if not text:
        return []
    if text.startswith("["):
        data = json.loads(text)
        if not isinstance(data, list):
            raise ValueError("JSON export must be a list of decision objects")
        return [r for r in data if isinstance(r, dict)]
    rows: list[dict[str, Any]] = []
    for line in text.splitlines():
        line = line.strip()
        if not line:
            continue
        obj = json.loads(line)
        if isinstance(obj, dict):
            rows.append(obj)
    return rows


def replay_rule_on_rows(
    rows: list[dict[str, Any]],
) -> tuple[list[dict[str, Any]], int, int]:
    """
    Re-run ``rule_based_decision`` (+ validate) on export rows.

    Returns (result_rows, replayed_count, skipped_count).
    Each result: inst_id, action, signal_diag, skipped?, reason?
    """
    results: list[dict[str, Any]] = []
    replayed = skipped = 0
    for row in rows:
        snap = snapshot_from_export_row(row)
        if snap is None:
            skipped += 1
            results.append(
                {
                    "inst_id": row.get("inst_id") or row.get("instrument"),
                    "action": None,
                    "signal_diag": None,
                    "skipped": True,
                    "reason": "incomplete_calculus_or_factors",
                    "original_action": row.get("action"),
                }
            )
            continue
        decision = validate_decision(rule_based_decision(snap))
        replayed += 1
        results.append(
            {
                "inst_id": decision.inst_id,
                "action": decision.action,
                "signal_diag": decision.signal_diag,
                "skipped": False,
                "original_action": row.get("action"),
            }
        )
    return results, replayed, skipped


def near_signal_rate(results: list[dict[str, Any]]) -> float:
    """Fraction of *replayed* WAIT rows with nearest in {long, short}."""
    considered = [r for r in results if not r.get("skipped")]
    if not considered:
        return 0.0
    near = 0
    for row in considered:
        if str(row.get("action") or "").upper() != "WAIT":
            continue
        diag = row.get("signal_diag") or {}
        if not isinstance(diag, dict):
            continue
        if str(diag.get("nearest") or "").lower() in ("long", "short"):
            near += 1
    return near / len(considered)


def action_histogram(results: list[dict[str, Any]]) -> dict[str, int]:
    from collections import Counter

    considered = [r for r in results if not r.get("skipped")]
    return dict(Counter(str(r.get("action") or "UNKNOWN") for r in considered))
