"""
Deterministic Stub / Rule decision policies for offline tests and paper cycles.

No LLM calls. Uses the same RSI/MACD/trend heuristics previously inline in
``keel.worker.cycle.rule_based_decision``.
"""
from __future__ import annotations

from keel.factors.market_data import MarketSnapshot
from keel.domain.decision import Decision, validate_decision
from keel.policy.protocol import DecisionPolicy, PolicyContext, PolicyResult


def rule_based_decision(snapshot: MarketSnapshot) -> Decision:
    """
    Deterministic paper decision (no LLM).

    Produces valid RR >= 2 geometry when a signal fires so the risk/execution
    path is exercised end-to-end.
    """
    if not snapshot.data_valid or snapshot.price <= 0 or snapshot.atr_14 <= 0:
        return Decision(inst_id=snapshot.inst_id, action="WAIT", reason="invalid market data")

    price = snapshot.price
    atr = snapshot.atr_14
    margin = 50.0

    if snapshot.rsi_14 <= 42 and snapshot.trend_15m == "bullish" and snapshot.macd_histogram >= 0:
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
            reason=f"paper long rsi={snapshot.rsi_14:.1f} trend={snapshot.trend_15m}",
        )

    if snapshot.rsi_14 >= 58 and snapshot.trend_15m == "bearish" and snapshot.macd_histogram <= 0:
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
            reason=f"paper short rsi={snapshot.rsi_14:.1f} trend={snapshot.trend_15m}",
        )

    return Decision(
        inst_id=snapshot.inst_id,
        action="WAIT",
        confidence=40.0,
        reason=f"no paper signal rsi={snapshot.rsi_14:.1f}",
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
    """Deterministic RSI/trend/MACD rules — default for paper cycles without LLM."""

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
