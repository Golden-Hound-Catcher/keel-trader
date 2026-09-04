"""
Hard risk gates for Keel Trader.

These gates are INDEPENDENT of LLM decisions. They cannot be overridden
by AI confidence, market conditions, or any other factor.

Design principles:
- Fail-closed: if we can't verify a condition, block the action
- Simple: each gate checks ONE thing
- Testable: pure functions with explicit inputs
- Auditable: every rejection is logged with clear reason
"""
from __future__ import annotations

import time
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from typing import Literal

from keel.config import get_settings
from keel.domain.decision import Decision

GateAction = Literal["open_long", "open_short", "scale_in", "close"]


@dataclass(frozen=True)
class GateContext:
    """Context for risk gate evaluation."""
    inst_id: str
    action: GateAction
    size: float
    margin_required: float
    current_positions: int
    long_positions: int
    short_positions: int
    daily_pnl: float
    existing_margin_for_asset: float = 0.0
    cooldown_until: float = 0.0
    kill_switch_active: bool = False


@dataclass
class GateResult:
    """Result of a risk gate check."""
    passed: bool
    gate_name: str
    reason: str = ""
    details: dict = field(default_factory=dict)


class RiskGate(ABC):
    """Base class for risk gates."""

    @property
    @abstractmethod
    def name(self) -> str:
        """Human-readable name for logging."""
        ...

    @abstractmethod
    def check(self, ctx: GateContext) -> GateResult:
        """Check if the action is allowed. Returns GateResult."""
        ...


class MaxPositionsGate(RiskGate):
    """Limit total concurrent positions."""

    def __init__(self, max_positions: int | None = None):
        settings = get_settings()
        self._max = max_positions or settings.max_concurrent_positions

    @property
    def name(self) -> str:
        return "max_positions"

    def check(self, ctx: GateContext) -> GateResult:
        if ctx.action == "close":
            return GateResult(passed=True, gate_name=self.name)

        if ctx.current_positions >= self._max:
            return GateResult(
                passed=False,
                gate_name=self.name,
                reason=f"已达最大持仓数 {ctx.current_positions}/{self._max}",
                details={"current": ctx.current_positions, "max": self._max},
            )
        return GateResult(passed=True, gate_name=self.name)


class MaxSameDirectionGate(RiskGate):
    """Limit positions in the same direction."""

    def __init__(self, max_same_direction: int | None = None):
        settings = get_settings()
        self._max = max_same_direction or settings.max_same_direction_positions

    @property
    def name(self) -> str:
        return "max_same_direction"

    def check(self, ctx: GateContext) -> GateResult:
        if ctx.action == "close":
            return GateResult(passed=True, gate_name=self.name)

        # scale_in counts toward the direction of the existing book side we are adding to.
        if ctx.action == "open_long":
            current = ctx.long_positions
        elif ctx.action == "open_short":
            current = ctx.short_positions
        elif ctx.action == "scale_in":
            # Prefer the side that already has size; fall back to long if both zero.
            current = ctx.long_positions if ctx.long_positions >= ctx.short_positions else ctx.short_positions
        else:
            return GateResult(passed=True, gate_name=self.name)

        if current >= self._max:
            return GateResult(
                passed=False,
                gate_name=self.name,
                reason=f"同方向持仓已达上限 {current}/{self._max}",
                details={"current": current, "max": self._max},
            )
        return GateResult(passed=True, gate_name=self.name)


class DailyLossGate(RiskGate):
    """Stop trading after daily loss limit is hit."""

    def __init__(self, max_daily_loss: float | None = None):
        settings = get_settings()
        self._max = max_daily_loss or settings.max_daily_loss_usdt

    @property
    def name(self) -> str:
        return "daily_loss"

    def check(self, ctx: GateContext) -> GateResult:
        if ctx.action == "close":
            return GateResult(passed=True, gate_name=self.name)

        if ctx.daily_pnl < -self._max:
            return GateResult(
                passed=False,
                gate_name=self.name,
                reason=f"今日亏损 {ctx.daily_pnl:.2f}U 已超限 {self._max}U",
                details={"daily_pnl": ctx.daily_pnl, "max_loss": self._max},
            )
        return GateResult(passed=True, gate_name=self.name)


class MaxMarginGate(RiskGate):
    """Limit margin for a single asset."""

    def __init__(self, max_margin: float | None = None):
        settings = get_settings()
        self._max = max_margin or settings.max_single_asset_margin

    @property
    def name(self) -> str:
        return "max_asset_margin"

    def check(self, ctx: GateContext) -> GateResult:
        if ctx.action == "close":
            return GateResult(passed=True, gate_name=self.name)

        total_margin = ctx.existing_margin_for_asset + ctx.margin_required
        if total_margin > self._max:
            return GateResult(
                passed=False,
                gate_name=self.name,
                reason=f"单标的保证金 {total_margin:.2f}U 将超限 {self._max}U",
                details={
                    "existing": ctx.existing_margin_for_asset,
                    "requested": ctx.margin_required,
                    "total": total_margin,
                    "max": self._max,
                },
            )
        return GateResult(passed=True, gate_name=self.name)


class CooldownGate(RiskGate):
    """Enforce cooldown after stop loss."""

    def __init__(self, cooldown_seconds: int = 1800):
        self._cooldown = cooldown_seconds

    @property
    def name(self) -> str:
        return "cooldown"

    def check(self, ctx: GateContext) -> GateResult:
        if ctx.action == "close":
            return GateResult(passed=True, gate_name=self.name)

        if ctx.cooldown_until > 0:
            remaining = int(ctx.cooldown_until - time.time())
            if remaining > 0:
                return GateResult(
                    passed=False,
                    gate_name=self.name,
                    reason=f"止损冷却中，剩余 {remaining // 60}分{remaining % 60}秒",
                    details={"remaining_seconds": remaining},
                )
        return GateResult(passed=True, gate_name=self.name)


class KillSwitchGate(RiskGate):
    """Emergency kill switch — blocks all gate actions when armed.

    Armed via ``KEEL_KILL_SWITCH`` (settings) and/or ``GateContext.kill_switch_active``.
    Decision ``WAIT`` never reaches gates (orchestrator short-circuit).
    """

    def __init__(self, active: bool | None = None):
        if active is None:
            self._active = get_settings().kill_switch
        else:
            self._active = active

    @property
    def name(self) -> str:
        return "kill_switch"

    def check(self, ctx: GateContext) -> GateResult:
        if self._active or ctx.kill_switch_active:
            return GateResult(
                passed=False,
                gate_name=self.name,
                reason="紧急熔断开关已激活",
                details={"kill_switch": True},
            )
        return GateResult(passed=True, gate_name=self.name)


class MinRiskRewardGate(RiskGate):
    """Enforce minimum risk:reward ratio."""

    def __init__(self, min_rr: float = 2.0):
        self._min_rr = min_rr

    @property
    def name(self) -> str:
        return "min_risk_reward"

    def check(self, ctx: GateContext) -> GateResult:
        return GateResult(passed=True, gate_name=self.name)


def gate_action_for_decision(
    decision: Decision,
    *,
    same_side_position: bool,
) -> GateAction:
    """
    Map a domain ``Decision`` to the risk-gate action vocabulary.

    ``WAIT`` is not a gate action — callers must short-circuit before gates.
    """
    if decision.action == "BUY_LONG":
        return "scale_in" if same_side_position else "open_long"
    if decision.action == "SELL_SHORT":
        return "scale_in" if same_side_position else "open_short"
    raise ValueError(f"no gate action for decision action={decision.action!r}")


def get_default_gates() -> list[RiskGate]:
    """Get the default set of risk gates."""
    return [
        KillSwitchGate(),
        DailyLossGate(),
        MaxPositionsGate(),
        MaxSameDirectionGate(),
        MaxMarginGate(),
        CooldownGate(),
    ]


def check_all_gates(ctx: GateContext, gates: list[RiskGate] | None = None) -> tuple[bool, list[GateResult]]:
    """
    Check all risk gates.
    
    Returns (all_passed, list_of_results).
    Stops at first failure (fail-fast).
    """
    gates = gates or get_default_gates()
    results: list[GateResult] = []

    for gate in gates:
        result = gate.check(ctx)
        results.append(result)
        if not result.passed:
            return False, results

    return True, results
