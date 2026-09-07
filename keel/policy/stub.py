"""
Deterministic Stub / Rule decision policies for offline tests and paper cycles.

No LLM calls. Rule v2: RSI + trend + MACD + EMA stack + volume_ratio filters.
"""
from __future__ import annotations

import os

from keel.factors.market_data import MarketSnapshot
from keel.domain.decision import Decision, validate_decision
from keel.policy.protocol import DecisionPolicy, PolicyContext, PolicyResult


def _env_float(key: str, default: float) -> float:
    raw = (os.environ.get(key) or "").strip()
    if not raw:
        return default
    try:
        return float(raw)
    except ValueError:
        return default


def _rule_thresholds() -> tuple[float, float, float]:
    """RSI long max, RSI short min, min volume_ratio (env-overridable)."""
    return (
        _env_float("KEEL_RULE_RSI_LONG_MAX", 42.0),
        _env_float("KEEL_RULE_RSI_SHORT_MIN", 58.0),
        _env_float("KEEL_RULE_MIN_VOLUME_RATIO", 1.0),
    )


def _factor_reason(snapshot: MarketSnapshot, prefix: str) -> str:
    return (
        f"{prefix} rsi={snapshot.rsi_14:.1f} trend={snapshot.trend_15m} "
        f"macd_h={snapshot.macd_histogram:.4f} "
        f"ema9={snapshot.ema_9:.4f} ema21={snapshot.ema_21:.4f} "
        f"vol={snapshot.volume_ratio:.2f}"
    )


def rule_based_decision(snapshot: MarketSnapshot) -> Decision:
    """
    Deterministic rule policy v2 (no LLM).

    Long: RSI ≤ long_max + bullish + MACD hist ≥ 0 + ema_9 ≥ ema_21 + vol ≥ min
    Short: RSI ≥ short_min + bearish + MACD hist ≤ 0 + ema_9 ≤ ema_21 + vol ≥ min

    Produces valid RR >= 2 geometry when a signal fires so the risk/execution
    path is exercised end-to-end. Offline-testable via crafted MarketSnapshot.
    """
    if not snapshot.data_valid or snapshot.price <= 0 or snapshot.atr_14 <= 0:
        return Decision(inst_id=snapshot.inst_id, action="WAIT", reason="invalid market data")

    price = snapshot.price
    atr = snapshot.atr_14
    margin = 50.0
    rsi_long_max, rsi_short_min, min_vol = _rule_thresholds()

    long_ok = (
        snapshot.rsi_14 <= rsi_long_max
        and snapshot.trend_15m == "bullish"
        and snapshot.macd_histogram >= 0
        and snapshot.ema_9 >= snapshot.ema_21
        and snapshot.volume_ratio >= min_vol
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
        )

    short_ok = (
        snapshot.rsi_14 >= rsi_short_min
        and snapshot.trend_15m == "bearish"
        and snapshot.macd_histogram <= 0
        and snapshot.ema_9 <= snapshot.ema_21
        and snapshot.volume_ratio >= min_vol
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
        )

    return Decision(
        inst_id=snapshot.inst_id,
        action="WAIT",
        confidence=40.0,
        reason=_factor_reason(snapshot, "no rule signal"),
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
