"""
Execution orchestrator for Keel Trader.

Handles the decision → risk check → order flow.
Limit-first execution strategy.
"""
from __future__ import annotations

import time
from dataclasses import dataclass
from typing import Literal

from keel.exchange.protocol import ExchangeProtocol, OrderRequest, OrderResult
from keel.risk.gates import GateContext, check_all_gates, RiskGate
from keel.ledger import KeelLedger, TradeRecord
from keel.llm.client import Decision


@dataclass
class ExecutionResult:
    """Result of executing a decision."""
    inst_id: str
    action: str
    success: bool
    order_id: str | None = None
    error: str | None = None
    risk_gate_failed: str | None = None
    price: float | None = None
    size: float | None = None


class ExecutionOrchestrator:
    """
    Orchestrates the execution of trading decisions.
    
    Flow:
    1. Receive decision from LLM
    2. Check all risk gates
    3. Calculate position size
    4. Submit limit order with TP/SL
    5. Record to ledger
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
        kill_switch: bool = False,
    ) -> ExecutionResult:
        """
        Execute a trading decision.
        
        Args:
            decision: The LLM decision to execute
            daily_pnl: Today's realized PnL
            cooldown_until: Timestamp when cooldown ends
            kill_switch: Whether emergency stop is active
            
        Returns:
            ExecutionResult
        """
        if decision.action == "WAIT":
            return ExecutionResult(
                inst_id=decision.inst_id,
                action="WAIT",
                success=True,
            )

        if not decision.valid:
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

        action_type: Literal["open_long", "open_short", "scale_in", "close"]
        if decision.action == "BUY_LONG":
            if current_position and current_position.side == "long":
                action_type = "scale_in"
            else:
                action_type = "open_long"
        else:
            if current_position and current_position.side == "short":
                action_type = "scale_in"
            else:
                action_type = "open_short"

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
            return ExecutionResult(
                inst_id=decision.inst_id,
                action=decision.action,
                success=False,
                risk_gate_failed=failed_gate.gate_name if failed_gate else "unknown",
                error=failed_gate.reason if failed_gate else "Risk gate failed",
            )

        ticker = self._exchange.get_ticker(decision.inst_id)
        if not ticker:
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

        if order_result.success:
            self._ledger.record_trade(
                TradeRecord(
                    timestamp=time.time(),
                    inst_id=decision.inst_id,
                    action="open" if not current_position else "scale_in",
                    direction="long" if decision.action == "BUY_LONG" else "short",
                    size=size,
                    price=entry_price,
                    strategy_tag="keel-llm",
                    reason=decision.reason,
                )
            )

        return ExecutionResult(
            inst_id=decision.inst_id,
            action=decision.action,
            success=order_result.success,
            order_id=order_result.order_id,
            error=order_result.error,
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
