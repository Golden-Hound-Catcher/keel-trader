"""
Execution orchestrator for Keel Trader.

Handles the decision → risk check → order flow.
Limit-first execution strategy.

Stage 4: Decision.valid (shared validate_decision) → risk gates → place order →
ledger trade only on fills (paper) / acceptance (live REST); resting limits and
failures become ledger events so API can audit the path.
"""
from __future__ import annotations

import time
from dataclasses import dataclass

from keel.config import get_settings
from keel.exchange.paper import PaperAdapter
from keel.exchange.protocol import ExchangeProtocol, OrderRequest, OrderResult
from keel.risk.gates import GateContext, check_all_gates, gate_action_for_decision, RiskGate
from keel.ledger import KeelLedger, TradeRecord
from keel.domain.decision import Decision, DecisionAction, validate_decision


@dataclass
class ExecutionResult:
    """Result of executing a decision."""
    inst_id: str
    action: DecisionAction | str
    success: bool
    order_id: str | None = None
    error: str | None = None
    risk_gate_failed: str | None = None
    price: float | None = None
    size: float | None = None
    filled: bool = False
    resting: bool = False


class ExecutionOrchestrator:
    """
    Orchestrates the execution of trading decisions.

    Flow:
    1. Receive decision (LLM or rule-based)
    2. Re-validate Decision schema / RR geometry
    3. Check all risk gates
    4. Calculate position size
    5. Submit limit order with TP/SL
    6. Record fill/trade or resting/failure events to ledger
    """

    def __init__(
        self,
        exchange: ExchangeProtocol,
        ledger: KeelLedger,
        risk_gates: list[RiskGate] | None = None,
    ):
        self._exchange = exchange
        self._ledger = ledger
        self._risk_gates = risk_gates

    def execute_decision(
        self,
        decision: Decision,
        daily_pnl: float = 0.0,
        cooldown_until: float = 0.0,
        kill_switch: bool | None = None,
    ) -> ExecutionResult:
        """
        Execute a trading decision.

        Args:
            decision: The LLM/rule decision to execute
            daily_pnl: Today's realized PnL
            cooldown_until: Timestamp when cooldown ends
            kill_switch: Emergency stop; None → ``settings.kill_switch`` (KEEL_KILL_SWITCH)

        Returns:
            ExecutionResult
        """
        if kill_switch is None:
            kill_switch = get_settings().kill_switch
        decision = validate_decision(decision)

        if decision.action == "WAIT":
            if not decision.valid and decision.validation_error:
                self._ledger.record_event(
                    "decision_invalid",
                    inst_id=decision.inst_id,
                    data={"error": decision.validation_error},
                )
                return ExecutionResult(
                    inst_id=decision.inst_id,
                    action="WAIT",
                    success=False,
                    error=decision.validation_error,
                )
            return ExecutionResult(
                inst_id=decision.inst_id,
                action="WAIT",
                success=True,
            )

        if not decision.valid:
            self._ledger.record_event(
                "decision_invalid",
                inst_id=decision.inst_id,
                data={"error": decision.validation_error or "Invalid decision"},
            )
            return ExecutionResult(
                inst_id=decision.inst_id,
                action=decision.action,
                success=False,
                error=decision.validation_error or "Invalid decision",
            )

        positions = self._exchange.get_positions()
        current_position = next(
            (p for p in positions if p.inst_id == decision.inst_id), None
        )

        position_count = len(positions)
        long_count = sum(1 for p in positions if p.side == "long")
        short_count = sum(1 for p in positions if p.side == "short")
        existing_margin = current_position.margin if current_position else 0.0

        same_side = bool(
            current_position
            and (
                (decision.action == "BUY_LONG" and current_position.side == "long")
                or (decision.action == "SELL_SHORT" and current_position.side == "short")
            )
        )
        action_type = gate_action_for_decision(decision, same_side_position=same_side)

        ctx = GateContext(
            inst_id=decision.inst_id,
            action=action_type,
            size=0,
            margin_required=decision.margin_usdt,
            current_positions=position_count,
            long_positions=long_count,
            short_positions=short_count,
            daily_pnl=daily_pnl,
            existing_margin_for_asset=existing_margin,
            cooldown_until=cooldown_until,
            kill_switch_active=kill_switch,
        )

        passed, results = check_all_gates(ctx, self._risk_gates)
        if not passed:
            failed_gate = next((r for r in results if not r.passed), None)
            gate_name = failed_gate.gate_name if failed_gate else "unknown"
            reason = failed_gate.reason if failed_gate else "Risk gate failed"
            self._ledger.record_event(
                "risk_gate_blocked",
                inst_id=decision.inst_id,
                data={"gate": gate_name, "error": reason, "action": decision.action},
            )
            return ExecutionResult(
                inst_id=decision.inst_id,
                action=decision.action,
                success=False,
                risk_gate_failed=gate_name,
                error=reason,
            )

        ticker = self._exchange.get_ticker(decision.inst_id)
        if not ticker:
            self._ledger.record_event(
                "order_failed",
                inst_id=decision.inst_id,
                data={"error": "Failed to get ticker data"},
            )
            return ExecutionResult(
                inst_id=decision.inst_id,
                action=decision.action,
                success=False,
                error="Failed to get ticker data",
            )

        entry_price = decision.entry_price or (
            ticker.bid if decision.action == "BUY_LONG" else ticker.ask
        )

        size = self._calculate_size(
            decision=decision,
            entry_price=entry_price,
        )

        if size <= 0:
            self._ledger.record_event(
                "order_failed",
                inst_id=decision.inst_id,
                data={"error": "Calculated size is zero"},
            )
            return ExecutionResult(
                inst_id=decision.inst_id,
                action=decision.action,
                success=False,
                error="Calculated size is zero",
            )

        order_result = self._exchange.place_order(
            OrderRequest(
                inst_id=decision.inst_id,
                side="buy" if decision.action == "BUY_LONG" else "sell",
                pos_side="long" if decision.action == "BUY_LONG" else "short",
                size=size,
                order_type="limit",
                price=entry_price,
                tp_trigger_price=decision.take_profit,
                sl_trigger_price=decision.stop_loss,
            )
        )

        return self._finalize_order(
            decision=decision,
            order_result=order_result,
            entry_price=entry_price,
            size=size,
            had_position=current_position is not None,
        )

    def _finalize_order(
        self,
        *,
        decision: Decision,
        order_result: OrderResult,
        entry_price: float,
        size: float,
        had_position: bool,
    ) -> ExecutionResult:
        """Ledger trade on fills; resting paper limits and failures become events."""
        if not order_result.success:
            self._ledger.record_event(
                "order_failed",
                inst_id=decision.inst_id,
                data={"error": order_result.error or "order rejected", "action": decision.action},
            )
            return ExecutionResult(
                inst_id=decision.inst_id,
                action=decision.action,
                success=False,
                order_id=order_result.order_id,
                error=order_result.error,
                price=entry_price,
                size=size,
            )

        order = order_result.order
        is_paper = isinstance(self._exchange, PaperAdapter)
        is_filled = (
            order is None
            or order.state == "filled"
            or order.filled_size > 0
        )
        # Paper resting limit (accepted but not filled) — do not invent a trade.
        if is_paper and order is not None and order.state == "live" and order.filled_size <= 0:
            self._ledger.record_event(
                "order_resting",
                inst_id=decision.inst_id,
                data={
                    "order_id": order_result.order_id,
                    "price": entry_price,
                    "size": size,
                    "action": decision.action,
                },
            )
            return ExecutionResult(
                inst_id=decision.inst_id,
                action=decision.action,
                success=True,
                order_id=order_result.order_id,
                price=entry_price,
                size=size,
                resting=True,
            )

        if is_filled or not is_paper:
            self._ledger.record_trade(
                TradeRecord(
                    timestamp=time.time(),
                    inst_id=decision.inst_id,
                    action="open" if not had_position else "scale_in",
                    direction="long" if decision.action == "BUY_LONG" else "short",
                    size=size,
                    price=entry_price,
                    strategy_tag="keel-llm",
                    reason=decision.reason,
                    metadata={
                        "order_id": order_result.order_id,
                        "leverage": decision.leverage,
                        "margin_usdt": decision.margin_usdt,
                        "take_profit": decision.take_profit,
                        "stop_loss": decision.stop_loss,
                    },
                )
            )
            self._ledger.record_event(
                "order_filled",
                inst_id=decision.inst_id,
                data={
                    "order_id": order_result.order_id,
                    "price": entry_price,
                    "size": size,
                    "action": decision.action,
                },
            )
            return ExecutionResult(
                inst_id=decision.inst_id,
                action=decision.action,
                success=True,
                order_id=order_result.order_id,
                price=entry_price,
                size=size,
                filled=True,
            )

        self._ledger.record_event(
            "order_accepted",
            inst_id=decision.inst_id,
            data={"order_id": order_result.order_id, "action": decision.action},
        )
        return ExecutionResult(
            inst_id=decision.inst_id,
            action=decision.action,
            success=True,
            order_id=order_result.order_id,
            price=entry_price,
            size=size,
        )

    def _calculate_size(
        self,
        decision: Decision,
        entry_price: float,
    ) -> float:
        """Calculate position size based on margin and leverage."""
        if decision.margin_usdt <= 0 or entry_price <= 0:
            return 0.0

        notional = decision.margin_usdt * decision.leverage
        return notional / entry_price
