"""
Deterministic Stub / Rule decision policies for offline tests and paper cycles.

No LLM calls. Rule v3: RSI + trend + MACD + EMA stack + adaptive volume_ratio
filters, with edge hints for near-probe observability.
Q0: diagnose_rule_signal attaches structured near-signal gate diagnostics.
"""
from __future__ import annotations

import os
from typing import Any

from keel.factors.market_data import MarketSnapshot
from keel.domain.decision import Decision, validate_decision
from keel.policy.protocol import DecisionPolicy, PolicyContext, PolicyResult

# Geometry mirrors near-probe / historical rule fires (TP 2.2 ATR, SL 1.0 ATR).
_TP_ATR = 2.2
_SL_ATR = 1.0
_RULE_GATE_COUNT = 5


def _env_float(key: str, default: float) -> float:
    raw = (os.environ.get(key) or "").strip()
    if not raw:
        return default
    try:
        return float(raw)
    except ValueError:
        return default


def _env_bool(key: str, default: bool) -> bool:
    raw = (os.environ.get(key) or "").strip().lower()
    if not raw:
        return default
    if raw in ("1", "true", "yes", "on"):
        return True
    if raw in ("0", "false", "no", "off"):
        return False
    return default


def _rule_thresholds() -> dict[str, float | bool]:
    """
    Rule v3 thresholds (env-overridable, backward-compatible KEEL_RULE_*).

    Default ``KEEL_RULE_MIN_VOLUME_RATIO`` lowered 1.0 → 0.5: live okx_public
    relative volume is right-skewed (p50≈0.36–0.40, p90≈0.86), so ≥1.0 almost
    never passed and dominated missing-gate histograms (~85%+). 0.5 sits above
    the median (filters dead bars) without requiring an above-average spike.
    """
    return {
        "rsi_long_max": _env_float("KEEL_RULE_RSI_LONG_MAX", 42.0),
        "rsi_short_min": _env_float("KEEL_RULE_RSI_SHORT_MIN", 58.0),
        # Hard volume floor (relative volume vs 20-bar mean).
        "min_vol": _env_float("KEEL_RULE_MIN_VOLUME_RATIO", 0.5),
        # Adaptive: also pass when last-bar percentile rank ≥ this (0 disables).
        "min_vol_percentile": _env_float("KEEL_RULE_MIN_VOLUME_PERCENTILE", 55.0),
        # Soft confirmation when other 4 gates + strong RSI extreme.
        "vol_soft_enable": _env_bool("KEEL_RULE_VOLUME_SOFT_ENABLE", True),
        "vol_soft_floor": _env_float("KEEL_RULE_VOLUME_SOFT_FLOOR", 0.35),
        "rsi_soft_long_max": _env_float("KEEL_RULE_RSI_SOFT_LONG_MAX", 35.0),
        "rsi_soft_short_min": _env_float("KEEL_RULE_RSI_SOFT_SHORT_MIN", 65.0),
    }


def _factor_reason(snapshot: MarketSnapshot, prefix: str) -> str:
    return (
        f"{prefix} rsi={snapshot.rsi_14:.1f} trend={snapshot.trend_15m} "
        f"macd_h={snapshot.macd_histogram:.4f} "
        f"ema9={snapshot.ema_9:.4f} ema21={snapshot.ema_21:.4f} "
        f"vol={snapshot.volume_ratio:.2f}"
    )


_LONG_GATES = (
    "rsi_long_ok",
    "trend_bullish",
    "macd_long_ok",
    "ema_long_ok",
    "volume_ok",
)
_SHORT_GATES = (
    "rsi_short_ok",
    "trend_bearish",
    "macd_short_ok",
    "ema_short_ok",
    "volume_ok",
)


def _edge_hints(snapshot: MarketSnapshot, n_missing: int) -> dict[str, float | None]:
    """ATR-based expected move / edge hint for probe observability (does not change fee hurdle)."""
    try:
        price = float(snapshot.price or 0.0)
        atr = float(snapshot.atr_14 or 0.0)
    except (TypeError, ValueError):
        return {"atr_bps": None, "expected_tp_bps": None, "edge_hint_bps": None}
    if price <= 0 or atr <= 0:
        return {"atr_bps": None, "expected_tp_bps": None, "edge_hint_bps": None}
    atr_bps = (atr / price) * 10_000.0
    expected_tp_bps = atr_bps * _TP_ATR
    completeness = max(
        0.0, min(1.0, (_RULE_GATE_COUNT - max(0, n_missing)) / float(_RULE_GATE_COUNT))
    )
    # Full fire → rule confidence 70; near-signal WAIT uses 40 — mirror for hint.
    conf_factor = 0.70 if n_missing == 0 else 0.40
    p = completeness * conf_factor
    ev_atr = (_TP_ATR * p) - (_SL_ATR * (1.0 - p))
    edge_hint = max(0.0, atr_bps * ev_atr)
    return {
        "atr_bps": float(atr_bps),
        "expected_tp_bps": float(expected_tp_bps),
        "edge_hint_bps": float(edge_hint),
    }


def _volume_gate(
    snapshot: MarketSnapshot,
    *,
    min_vol: float,
    min_vol_percentile: float,
    soft_enable: bool,
    soft_floor: float,
    other_four_ok: bool,
    rsi_extreme: bool,
) -> tuple[bool, float, bool, str]:
    """
    Evaluate volume_ok with hard / adaptive / soft paths.

    Returns (volume_ok, threshold_used, soft_pass, path).
    path ∈ {hard, percentile, soft, fail}.
    """
    ratio = float(snapshot.volume_ratio or 0.0)
    pct = getattr(snapshot, "volume_percentile", None)
    try:
        pct_f = float(pct) if pct is not None else None
    except (TypeError, ValueError):
        pct_f = None

    if ratio >= min_vol:
        return True, float(min_vol), False, "hard"

    if min_vol_percentile > 0 and pct_f is not None and pct_f >= min_vol_percentile:
        return True, float(min_vol_percentile), False, "percentile"

    if (
        soft_enable
        and other_four_ok
        and rsi_extreme
        and ratio >= soft_floor
    ):
        return True, float(soft_floor), True, "soft"

    # Threshold reported for diagnostics: hard floor (what still blocks).
    return False, float(min_vol), False, "fail"


def diagnose_rule_signal(snapshot: MarketSnapshot) -> dict[str, Any]:
    """
    Pure helper: structured rule-gate diagnostics for near-signal UX.

    Returns boolean gates, numeric snapshots, and ``nearest`` / ``missing``
    for the side closest to firing (fewer failed gates). Used by
    ``rule_based_decision`` (DRY) and unit-tested with crafted snapshots.

    Rule v3 extras: ``volume_threshold``, ``volume_soft_pass``, ``volume_path``,
    ``volume_percentile``, ``near_ready``, ``atr_bps`` / ``expected_tp_bps`` /
    ``edge_hint_bps``.
    """
    th = _rule_thresholds()
    rsi_long_max = float(th["rsi_long_max"])
    rsi_short_min = float(th["rsi_short_min"])
    min_vol = float(th["min_vol"])
    min_vol_percentile = float(th["min_vol_percentile"])
    soft_enable = bool(th["vol_soft_enable"])
    soft_floor = float(th["vol_soft_floor"])
    rsi_soft_long_max = float(th["rsi_soft_long_max"])
    rsi_soft_short_min = float(th["rsi_soft_short_min"])

    data_ok = bool(snapshot.data_valid) and snapshot.price > 0 and snapshot.atr_14 > 0

    rsi_long_ok = snapshot.rsi_14 <= rsi_long_max
    rsi_short_ok = snapshot.rsi_14 >= rsi_short_min
    trend_bullish = snapshot.trend_15m == "bullish"
    trend_bearish = snapshot.trend_15m == "bearish"
    macd_long_ok = snapshot.macd_histogram >= 0
    macd_short_ok = snapshot.macd_histogram <= 0
    ema_long_ok = snapshot.ema_9 >= snapshot.ema_21
    ema_short_ok = snapshot.ema_9 <= snapshot.ema_21

    long_four = rsi_long_ok and trend_bullish and macd_long_ok and ema_long_ok
    short_four = rsi_short_ok and trend_bearish and macd_short_ok and ema_short_ok
    # Soft RSI extreme: tighter than the hard RSI band.
    rsi_extreme_long = snapshot.rsi_14 <= rsi_soft_long_max
    rsi_extreme_short = snapshot.rsi_14 >= rsi_soft_short_min

    # Hard/percentile paths ignore side context; soft path needs other-4 + RSI extreme.
    # Try long soft context first, then short (identical for hard/percentile).
    candidates = [
        _volume_gate(
            snapshot,
            min_vol=min_vol,
            min_vol_percentile=min_vol_percentile,
            soft_enable=soft_enable,
            soft_floor=soft_floor,
            other_four_ok=long_four,
            rsi_extreme=rsi_extreme_long,
        ),
        _volume_gate(
            snapshot,
            min_vol=min_vol,
            min_vol_percentile=min_vol_percentile,
            soft_enable=soft_enable,
            soft_floor=soft_floor,
            other_four_ok=short_four,
            rsi_extreme=rsi_extreme_short,
        ),
    ]
    # Prefer hard > percentile > soft > fail for diagnostics.
    _path_rank = {"hard": 0, "percentile": 1, "soft": 2, "fail": 3}
    best = min(candidates, key=lambda c: (_path_rank.get(c[3], 9), -int(c[0])))
    volume_ok, volume_threshold, volume_soft_pass, volume_path = best

    vol_pct = getattr(snapshot, "volume_percentile", None)
    try:
        vol_pct_out: float | None = float(vol_pct) if vol_pct is not None else None
    except (TypeError, ValueError):
        vol_pct_out = None

    gates: dict[str, Any] = {
        "data_valid": data_ok,
        "rsi_long_ok": rsi_long_ok,
        "rsi_short_ok": rsi_short_ok,
        "trend_bullish": trend_bullish,
        "trend_bearish": trend_bearish,
        "macd_long_ok": macd_long_ok,
        "macd_short_ok": macd_short_ok,
        "ema_long_ok": ema_long_ok,
        "ema_short_ok": ema_short_ok,
        "volume_ok": volume_ok,
        "rsi_14": snapshot.rsi_14,
        "volume_ratio": snapshot.volume_ratio,
        "volume_threshold": volume_threshold,
        "volume_soft_pass": volume_soft_pass,
        "volume_path": volume_path,
        "volume_percentile": vol_pct_out,
        "ema_9": snapshot.ema_9,
        "ema_21": snapshot.ema_21,
        "macd_histogram": snapshot.macd_histogram,
        "trend_15m": snapshot.trend_15m,
    }

    if not data_ok:
        gates["nearest"] = "none"
        gates["missing"] = ["data_valid"]
        gates["near_ready"] = False
        gates.update(_edge_hints(snapshot, _RULE_GATE_COUNT))
        return gates

    long_missing = [g for g in _LONG_GATES if not gates[g]]
    short_missing = [g for g in _SHORT_GATES if not gates[g]]
    n_long, n_short = len(long_missing), len(short_missing)

    if n_long == 0 and n_short == 0:
        nearest, missing = "long", []
    elif n_long == 0:
        nearest, missing = "long", []
    elif n_short == 0:
        nearest, missing = "short", []
    elif n_long < n_short:
        nearest, missing = "long", long_missing
    elif n_short < n_long:
        nearest, missing = "short", short_missing
    else:
        nearest, missing = "long", long_missing

    gates["nearest"] = nearest
    gates["missing"] = missing
    # Staged near_ready: only volume blocks, but ratio clears soft floor (no trade by itself).
    gates["near_ready"] = (
        missing == ["volume_ok"]
        and float(snapshot.volume_ratio or 0.0) >= soft_floor
    )
    gates.update(_edge_hints(snapshot, len(missing)))
    return gates


def rule_based_decision(snapshot: MarketSnapshot) -> Decision:
    """
    Deterministic rule policy v3 (no LLM).

    Long: RSI ≤ long_max + bullish + MACD hist ≥ 0 + ema_9 ≥ ema_21 + volume_ok
    Short: RSI ≥ short_min + bearish + MACD hist ≤ 0 + ema_9 ≤ ema_21 + volume_ok

    ``volume_ok`` (Rule v3): ratio ≥ min_vol (default 0.5), OR last-bar volume
    percentile ≥ min percentile (default 55), OR soft confirmation when the
    other four gates pass with a strong RSI extreme and ratio ≥ soft floor.

    Produces valid RR >= 2 geometry when a signal fires so the risk/execution
    path is exercised end-to-end. Offline-testable via crafted MarketSnapshot.
    Attaches ``signal_diag`` (from ``diagnose_rule_signal``) on every decision.
    Near-probe fee hurdle (10 bps taker RT) is unchanged — edge hints are audit-only.
    """
    diag = diagnose_rule_signal(snapshot)

    if not diag["data_valid"]:
        return Decision(
            inst_id=snapshot.inst_id,
            action="WAIT",
            reason="invalid market data",
            signal_diag=diag,
        )

    price = snapshot.price
    atr = snapshot.atr_14
    margin = 50.0

    long_ok = (
        diag["rsi_long_ok"]
        and diag["trend_bullish"]
        and diag["macd_long_ok"]
        and diag["ema_long_ok"]
        and diag["volume_ok"]
    )
    if long_ok:
        entry = price
        sl = entry - 1.0 * atr
        tp = entry + 2.2 * atr
        return Decision(
            inst_id=snapshot.inst_id,
            action="BUY_LONG",
            confidence=70.0,
            entry_price=entry,
            take_profit=tp,
            stop_loss=sl,
            leverage=3,
            margin_usdt=margin,
            reason=_factor_reason(snapshot, "rule long"),
            signal_diag=diag,
        )

    short_ok = (
        diag["rsi_short_ok"]
        and diag["trend_bearish"]
        and diag["macd_short_ok"]
        and diag["ema_short_ok"]
        and diag["volume_ok"]
    )
    if short_ok:
        entry = price
        sl = entry + 1.0 * atr
        tp = entry - 2.2 * atr
        return Decision(
            inst_id=snapshot.inst_id,
            action="SELL_SHORT",
            confidence=70.0,
            entry_price=entry,
            take_profit=tp,
            stop_loss=sl,
            leverage=3,
            margin_usdt=margin,
            reason=_factor_reason(snapshot, "rule short"),
            signal_diag=diag,
        )

    return Decision(
        inst_id=snapshot.inst_id,
        action="WAIT",
        confidence=40.0,
        reason=_factor_reason(snapshot, "no rule signal"),
        signal_diag=diag,
    )


class StubDecisionPolicy:
    """
    Offline / test policy: always WAIT (ignores market data).

    Useful when tests need a no-op decision path without exercising rules.
    """

    @property
    def name(self) -> str:
        return "stub"

    def decide(self, ctx: PolicyContext) -> PolicyResult:
        decisions = {
            inst_id: Decision(inst_id=inst_id, action="WAIT", reason="stub policy")
            for inst_id in ctx.instrument_ids
        }
        return PolicyResult(decisions=decisions, policy_name=self.name, success=True)


class RuleDecisionPolicy:
    """Deterministic RSI/trend/MACD/EMA/vol rules — default without LLM."""

    @property
    def name(self) -> str:
        return "rule"

    def decide(self, ctx: PolicyContext) -> PolicyResult:
        decisions: dict[str, Decision] = {}
        for inst_id in ctx.instrument_ids:
            snap = ctx.snapshots.get(inst_id)
            if snap is None:
                decisions[inst_id] = Decision(
                    inst_id=inst_id,
                    action="WAIT",
                    reason="missing snapshot",
                    valid=False,
                    validation_error="missing snapshot",
                )
                continue
            decisions[inst_id] = validate_decision(rule_based_decision(snap))
        return PolicyResult(decisions=decisions, policy_name=self.name, success=True)


# Protocol conformance (static typing helpers; runtime_checkable also works)
_STUB_CHECK: DecisionPolicy = StubDecisionPolicy()
_RULE_CHECK: DecisionPolicy = RuleDecisionPolicy()
