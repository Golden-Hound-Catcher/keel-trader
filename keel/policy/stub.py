"""
Deterministic Stub / Rule decision policies for offline tests and paper cycles.

No LLM calls. Rule v3+: RSI + trend + MACD + EMA stack + adaptive volume_ratio
filters, soft RSI relax when the other four gates pass, and edge hints for
near-probe observability.
R5: real multi-TF trends (trend_15m/1h/4h); entry gate is 15m; optional hard
1h alignment via KEEL_RULE_REQUIRE_1H_TREND (default 0 = soft confirm only).
R6: when trend_1h_confirm and nearest side aligns, multiply edge_hint_bps by
KEEL_RULE_1H_EDGE_BOOST (default 1.25x, clamped 1.0–2.0; uplift capped +5 bps).
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
# R6: 1h-confirm edge_hint multiplier bounds + absolute uplift cap (bps).
_1H_EDGE_BOOST_DEFAULT = 1.25
_1H_EDGE_BOOST_MIN = 1.0
_1H_EDGE_BOOST_MAX = 2.0
_1H_EDGE_BOOST_CAP_BPS = 5.0


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
    Rule v3+ thresholds (env-overridable, backward-compatible KEEL_RULE_*).

    Default ``KEEL_RULE_MIN_VOLUME_RATIO`` lowered 1.0 → 0.5: live okx_public
    relative volume is right-skewed (p50≈0.36–0.40, p90≈0.86), so ≥1.0 almost
    never passed and dominated missing-gate histograms (~85%+). 0.5 sits above
    the median (filters dead bars) without requiring an above-average spike.

    After v3, okx_public missing shifts to RSI side gates (esp. ``rsi_short_ok``):
    observed RSI sits mid-band (p50≈45–46, max often <50), so hard short ≥58
    almost never clears. Defaults widen modestly 42/58 → 45/55, and a soft RSI
    relax path (other four gates + slightly looser band) mirrors volume soft
    confirm without flooding every bar.
    """
    return {
        # Hard RSI bands (mean-reversion): modestly widened after v3 RSI bottleneck.
        "rsi_long_max": _env_float("KEEL_RULE_RSI_LONG_MAX", 45.0),
        "rsi_short_min": _env_float("KEEL_RULE_RSI_SHORT_MIN", 55.0),
        # Hard volume floor (relative volume vs 20-bar mean).
        "min_vol": _env_float("KEEL_RULE_MIN_VOLUME_RATIO", 0.5),
        # Adaptive: also pass when last-bar percentile rank ≥ this (0 disables).
        "min_vol_percentile": _env_float("KEEL_RULE_MIN_VOLUME_PERCENTILE", 55.0),
        # Soft volume confirmation when other 4 gates + strong RSI extreme.
        "vol_soft_enable": _env_bool("KEEL_RULE_VOLUME_SOFT_ENABLE", True),
        "vol_soft_floor": _env_float("KEEL_RULE_VOLUME_SOFT_FLOOR", 0.35),
        "rsi_soft_long_max": _env_float("KEEL_RULE_RSI_SOFT_LONG_MAX", 35.0),
        "rsi_soft_short_min": _env_float("KEEL_RULE_RSI_SOFT_SHORT_MIN", 65.0),
        # Soft RSI relax: other 4 (trend/macd/ema/volume) + looser RSI band.
        "rsi_relax_enable": _env_bool("KEEL_RULE_RSI_RELAX_ENABLE", True),
        "rsi_relax_long_max": _env_float("KEEL_RULE_RSI_RELAX_LONG_MAX", 48.0),
        "rsi_relax_short_min": _env_float("KEEL_RULE_RSI_RELAX_SHORT_MIN", 52.0),
        # R5: hard-require 1h trend same direction as 15m (default off = soft confirm).
        "require_1h_trend": _env_bool("KEEL_RULE_REQUIRE_1H_TREND", False),
        # R6: multiplicative edge_hint boost when 1h confirms nearest side (default 1.25x).
        "edge_1h_boost": _env_float("KEEL_RULE_1H_EDGE_BOOST", _1H_EDGE_BOOST_DEFAULT),
    }


def _factor_reason(snapshot: MarketSnapshot, prefix: str) -> str:
    t1h = getattr(snapshot, "trend_1h", "neutral") or "neutral"
    t4h = getattr(snapshot, "trend_4h", "neutral") or "neutral"
    return (
        f"{prefix} rsi={snapshot.rsi_14:.1f} "
        f"trend15m={snapshot.trend_15m} trend1h={t1h} trend4h={t4h} "
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


def _clamp_1h_edge_boost(raw: float) -> float:
    """Clamp KEEL_RULE_1H_EDGE_BOOST into [1.0, 2.0] so ops cannot invent huge edges."""
    try:
        m = float(raw)
    except (TypeError, ValueError):
        m = _1H_EDGE_BOOST_DEFAULT
    if m != m:  # NaN
        return _1H_EDGE_BOOST_DEFAULT
    return max(_1H_EDGE_BOOST_MIN, min(_1H_EDGE_BOOST_MAX, m))


def apply_1h_edge_boost(
    hints: dict[str, float | None],
    *,
    trend_1h_confirm: bool,
    nearest: str,
    trend_15m: str,
    boost_mult: float,
) -> dict[str, Any]:
    """
    R6: multiply edge_hint_bps when 1h confirms and nearest side aligns with 15m.

    Formula (documented):
      mult = clamp(KEEL_RULE_1H_EDGE_BOOST, 1.0, 2.0)  # default 1.25
      if trend_1h_confirm and nearest∈{long,short} aligns with trend_15m:
          boosted = base * mult
          edge_hint_bps = min(boosted, base + 5.0)   # absolute uplift cap
      else:
          edge_hint_bps = base

    Does **not** change the near-probe fee hurdle (~10 bps taker RT).
    """
    out: dict[str, Any] = dict(hints)
    base = hints.get("edge_hint_bps")
    mult = _clamp_1h_edge_boost(boost_mult)
    out["edge_hint_boost_mult"] = float(mult)
    out["edge_hint_1h_boosted"] = False
    if base is None:
        return out
    try:
        base_f = float(base)
    except (TypeError, ValueError):
        return out
    if base_f != base_f or base_f < 0:
        return out

    nearest_s = str(nearest or "")
    t15 = str(trend_15m or "neutral")
    aligns = (
        bool(trend_1h_confirm)
        and (
            (nearest_s == "long" and t15 == "bullish")
            or (nearest_s == "short" and t15 == "bearish")
        )
    )
    if not aligns or mult <= 1.0:
        out["edge_hint_bps"] = float(base_f)
        return out

    boosted = base_f * mult
    # Cap absolute uplift so a large mult cannot invent huge edges.
    capped = min(boosted, base_f + _1H_EDGE_BOOST_CAP_BPS)
    # Never claim more edge than the expected TP move.
    etp = hints.get("expected_tp_bps")
    try:
        etp_f = float(etp) if etp is not None else None
    except (TypeError, ValueError):
        etp_f = None
    if etp_f is not None and etp_f == etp_f and etp_f > 0:
        capped = min(capped, etp_f)
    out["edge_hint_bps"] = float(max(0.0, capped))
    out["edge_hint_1h_boosted"] = True
    out["edge_hint_bps_raw"] = float(base_f)
    return out


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

    Rule v3+ extras: ``volume_threshold``, ``volume_soft_pass``, ``volume_path``,
    ``volume_percentile``, ``rsi_soft_pass``, ``rsi_path``, ``near_ready``,
    ``atr_bps`` / ``expected_tp_bps`` / ``edge_hint_bps``.
    R5: ``trend_15m`` / ``trend_1h`` / ``trend_4h``, ``trend_gate``
    (``15m`` or ``15m+1h``), ``trend_1h_confirm``, ``require_1h_trend``.
    R6: optional 1h-confirm ``edge_hint`` boost (``edge_hint_1h_boosted``,
    ``edge_hint_boost_mult``, ``edge_hint_bps_raw`` when applied).
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
    rsi_relax_enable = bool(th["rsi_relax_enable"])
    rsi_relax_long_max = float(th["rsi_relax_long_max"])
    rsi_relax_short_min = float(th["rsi_relax_short_min"])

    data_ok = bool(snapshot.data_valid) and snapshot.price > 0 and snapshot.atr_14 > 0

    rsi_long_hard = snapshot.rsi_14 <= rsi_long_max
    rsi_short_hard = snapshot.rsi_14 >= rsi_short_min
    require_1h = bool(th["require_1h_trend"])
    trend_15m = str(snapshot.trend_15m or "neutral")
    trend_1h = str(getattr(snapshot, "trend_1h", "neutral") or "neutral")
    trend_4h = str(getattr(snapshot, "trend_4h", "neutral") or "neutral")
    # Entry gate is always 15m; optional hard 1h same-direction confirmation.
    trend_15m_bullish = trend_15m == "bullish"
    trend_15m_bearish = trend_15m == "bearish"
    trend_1h_confirm_long = trend_1h == "bullish"
    trend_1h_confirm_short = trend_1h == "bearish"
    if require_1h:
        trend_bullish = trend_15m_bullish and trend_1h_confirm_long
        trend_bearish = trend_15m_bearish and trend_1h_confirm_short
        trend_gate = "15m+1h"
    else:
        trend_bullish = trend_15m_bullish
        trend_bearish = trend_15m_bearish
        trend_gate = "15m"
    # Soft confirm flag: 1h matches directional 15m (audit only when require=0).
    if trend_15m_bullish:
        trend_1h_confirm = trend_1h_confirm_long
    elif trend_15m_bearish:
        trend_1h_confirm = trend_1h_confirm_short
    else:
        trend_1h_confirm = False
    macd_long_ok = snapshot.macd_histogram >= 0
    macd_short_ok = snapshot.macd_histogram <= 0
    ema_long_ok = snapshot.ema_9 >= snapshot.ema_21
    ema_short_ok = snapshot.ema_9 <= snapshot.ema_21

    # Volume soft uses *hard* RSI in the other-four (same as pre-relax).
    long_four = rsi_long_hard and trend_bullish and macd_long_ok and ema_long_ok
    short_four = rsi_short_hard and trend_bearish and macd_short_ok and ema_short_ok
    # Soft RSI extreme: tighter than the hard RSI band (for volume soft).
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

    # Soft RSI relax (after volume): other four without RSI + looser band.
    # Mirrors volume soft confirm but loosens RSI when trend/macd/ema/volume align.
    long_other4 = trend_bullish and macd_long_ok and ema_long_ok and volume_ok
    short_other4 = trend_bearish and macd_short_ok and ema_short_ok and volume_ok
    rsi_long_soft = (
        rsi_relax_enable
        and long_other4
        and snapshot.rsi_14 <= rsi_relax_long_max
    )
    rsi_short_soft = (
        rsi_relax_enable
        and short_other4
        and snapshot.rsi_14 >= rsi_relax_short_min
    )
    rsi_long_ok = rsi_long_hard or rsi_long_soft
    rsi_short_ok = rsi_short_hard or rsi_short_soft
    if rsi_long_hard or rsi_short_hard:
        rsi_path = "hard"
        rsi_soft_pass = False
    elif rsi_long_soft or rsi_short_soft:
        rsi_path = "soft"
        rsi_soft_pass = True
    else:
        rsi_path = "fail"
        rsi_soft_pass = False

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
        "rsi_soft_pass": rsi_soft_pass,
        "rsi_path": rsi_path,
        "ema_9": snapshot.ema_9,
        "ema_21": snapshot.ema_21,
        "macd_histogram": snapshot.macd_histogram,
        "trend_15m": trend_15m,
        "trend_1h": trend_1h,
        "trend_4h": trend_4h,
        "trend_gate": trend_gate,
        "trend_1h_confirm": trend_1h_confirm,
        "require_1h_trend": require_1h,
    }

    if not data_ok:
        gates["nearest"] = "none"
        gates["missing"] = ["data_valid"]
        gates["near_ready"] = False
        gates.update(
            apply_1h_edge_boost(
                _edge_hints(snapshot, _RULE_GATE_COUNT),
                trend_1h_confirm=False,
                nearest="none",
                trend_15m=trend_15m,
                boost_mult=float(th["edge_1h_boost"]),
            )
        )
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
    # Staged near_ready: only volume blocks (soft floor) OR only RSI blocks within relax band.
    near_vol = (
        missing == ["volume_ok"]
        and float(snapshot.volume_ratio or 0.0) >= soft_floor
    )
    near_rsi_long = (
        missing == ["rsi_long_ok"]
        and rsi_relax_enable
        and snapshot.rsi_14 <= rsi_relax_long_max
    )
    near_rsi_short = (
        missing == ["rsi_short_ok"]
        and rsi_relax_enable
        and snapshot.rsi_14 >= rsi_relax_short_min
    )
    gates["near_ready"] = near_vol or near_rsi_long or near_rsi_short
    gates.update(
        apply_1h_edge_boost(
            _edge_hints(snapshot, len(missing)),
            trend_1h_confirm=bool(trend_1h_confirm),
            nearest=str(nearest),
            trend_15m=trend_15m,
            boost_mult=float(th["edge_1h_boost"]),
        )
    )
    return gates


def rule_based_decision(snapshot: MarketSnapshot) -> Decision:
    """
    Deterministic rule policy v3+ (no LLM).

    Long: RSI ≤ long_max + bullish(15m[+1h]) + MACD hist ≥ 0 + ema_9 ≥ ema_21 + volume_ok
    Short: RSI ≥ short_min + bearish(15m[+1h]) + MACD hist ≤ 0 + ema_9 ≤ ema_21 + volume_ok

    Trend entry gate is ``trend_15m``; set ``KEEL_RULE_REQUIRE_1H_TREND=1`` to also
    require ``trend_1h`` same direction (default 0 keeps soft confirm in signal_diag only).

    ``volume_ok`` (Rule v3): ratio ≥ min_vol (default 0.5), OR last-bar volume
    percentile ≥ min percentile (default 55), OR soft confirmation when the
    other four gates pass with a strong RSI extreme and ratio ≥ soft floor.

    ``rsi_*_ok`` soft relax (v3+): when trend+macd+ema+volume already pass,
    RSI may clear via a slightly looser band (default long ≤48 / short ≥52).

    Produces valid RR >= 2 geometry when a signal fires so the risk/execution
    path is exercised end-to-end. Offline-testable via crafted MarketSnapshot.
    Attaches ``signal_diag`` (from ``diagnose_rule_signal``) on every decision.
    Near-probe fee hurdle (10 bps taker RT) is unchanged — edge hints are audit-only.
    R6 may boost ``edge_hint_bps`` when 1h confirms nearest side (still does not
    lower the fee hurdle).
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
