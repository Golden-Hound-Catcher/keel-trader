"""
Trading Decision — shared by policy, LLM client, and execution.

Owns the in-memory decision shape used on the happy path. Persistence uses
``DecisionRecord`` in ``keel.domain.records``.
"""
from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal, ROUND_HALF_UP
from typing import Any, Literal

DecisionAction = Literal["BUY_LONG", "SELL_SHORT", "WAIT"]


@dataclass
class Decision:
    """Parsed / constructed trading decision."""

    inst_id: str
    action: DecisionAction
    confidence: float = 0.0
    entry_price: float | None = None
    take_profit: float | None = None
    stop_loss: float | None = None
    leverage: int = 3
    margin_usdt: float = 0.0
    reason: str = ""
    valid: bool = True
    validation_error: str = ""
    # Q0 near-signal diagnostics (rule policy); persisted under calculus_data.signal_diag
    signal_diag: dict[str, Any] | None = None


def validate_decision(decision: Decision, *, min_rr: float = 2.0) -> Decision:
    """
    Validate Decision geometry and minimum risk:reward.

    Shared by LLM parsing and the paper/demo cycle so schema → risk stays coherent.
    Invalid decisions are rewritten to WAIT with valid=False.
    Preserves ``signal_diag`` / ``reason`` when rewriting.
    """
    if decision.action == "WAIT":
        return decision

    entry = decision.entry_price or 0
    tp = decision.take_profit or 0
    sl = decision.stop_loss or 0

    def _wait(err: str) -> Decision:
        return Decision(
            inst_id=decision.inst_id,
            action="WAIT",
            reason=decision.reason,
            valid=False,
            validation_error=err,
            signal_diag=decision.signal_diag,
        )

    if decision.action == "BUY_LONG":
        if not (sl < entry < tp):
            return _wait(f"Invalid price geometry for long: SL={sl}, Entry={entry}, TP={tp}")
        rr = (tp - entry) / (entry - sl) if entry > sl else 0
    else:
        if not (tp < entry < sl):
            return _wait(f"Invalid price geometry for short: TP={tp}, Entry={entry}, SL={sl}")
        rr = (entry - tp) / (sl - entry) if sl > entry else 0

    # Compare at 2 decimal places so a displayed 2.00 is not rejected as 1.996.
    rr_q = _quantize_rr(rr)
    min_q = _quantize_rr(min_rr)
    if rr_q < min_q:
        return _wait(f"Risk:reward {rr_q} below minimum {min_q}")

    return decision


def _quantize_rr(value: float) -> Decimal:
    return Decimal(value).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)
