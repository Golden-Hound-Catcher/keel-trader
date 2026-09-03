"""
Typed Protocol for trading decision policies.

DecisionPolicy is the replaceable port used by the worker cycle.
Implementations: Stub/Rule (deterministic, offline) and LLM JSON policy.
Risk gates remain outside this port — they cannot be overridden by any policy.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Protocol, runtime_checkable

from keel.factors.market_data import MarketSnapshot
from keel.domain.decision import Decision


@dataclass
class PolicyContext:
    """Inputs available to a decision policy for one cycle."""

    snapshots: dict[str, MarketSnapshot]
    instrument_ids: list[str]
    timestamp: float = 0.0
    extras: dict[str, Any] = field(default_factory=dict)


@dataclass
class PolicyResult:
    """Outputs from a decision policy for one cycle."""

    decisions: dict[str, Decision]
    policy_name: str = ""
    macro_assessment: str = ""
    prompt_meta: dict[str, Any] = field(default_factory=dict)
    error: str = ""
    success: bool = True


@runtime_checkable
class DecisionPolicy(Protocol):
    """
    Replaceable decision port.

    Worker cycle calls ``decide`` once per tick with market snapshots.
    Implementations must return one Decision per requested instrument id
    (WAIT is fine). Geometry / RR validation should use
    ``keel.domain.decision.validate_decision``.
    """

    @property
    def name(self) -> str:
        """Stable adapter label for logs / cycle summary."""
        ...

    def decide(self, ctx: PolicyContext) -> PolicyResult:
        """Produce trading decisions for all instruments in ctx."""
        ...
