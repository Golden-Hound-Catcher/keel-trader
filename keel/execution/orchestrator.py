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
from dataclasses import dataclass, replace

from keel.config import get_settings
from keel.exchange.paper import PaperAdapter
from keel.exchange.protocol import ExchangeProtocol, OrderRequest, OrderResult
from keel.risk.gates import GateContext, check_all_gates, gate_action_for_decision, RiskGate
from keel.ledger import KeelLedger, TradeRecord
from keel.domain.decision import Decision, DecisionAction, validate_decision
from keel.execution.near_probe import (
    PROBE_POLICY,
    PROBE_STRATEGY_TAG,
    is_probe_decision,
    probe_audit_fields,
)
from keel.execution.provenance import provenance_fields
from keel.domain.instruments import lookup_instrument, notional_from_size
from keel.execution.sizing import SizeConstraints, size_order


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
    shadow: bool = False


class ExecutionOrchestrator:
    """
    Orchestrates the execution of trading decisions.

    Flow:
    1. Receive decision (LLM or rule-based)
    2. Re-validate Decision schema / RR geometry
    3. Size from margin×leverage using contract face value, clip to remaining caps
    4. Check all risk gates (kill-switch blocks real orders; shadow may proceed)
    5. If shadow_mode: ledger shadow_fill (+ optional synthetic trade); no place_order
    6. Else submit limit order with TP/SL and record fill/resting/failure
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
        shadow_mode: bool | None = None,
    ) -> ExecutionResult:
        """
        Execute a trading decision.

        Args:
            decision: The LLM/rule decision to execute
            daily_pnl: Today's realized PnL
            cooldown_until: Timestamp when cooldown ends
            kill_switch: Emergency stop; None → ``settings.kill_switch`` (KEEL_KILL_SWITCH)
            shadow_mode: When true, ledger shadow fill instead of exchange place_order;
                None → ``settings.shadow_mode`` (KEEL_SHADOW_MODE). Kill-switch blocks
                real orders only — shadow rehearsal proceeds when both are on.

        Returns:
            ExecutionResult
        """
        settings = get_settings()
        if kill_switch is None:
            kill_switch = settings.kill_switch
        if shadow_mode is None:
            shadow_mode = settings.shadow_mode
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
        existing_size = current_position.size if current_position else 0.0

        same_side = bool(
            current_position
            and (
                (decision.action == "BUY_LONG" and current_position.side == "long")
                or (decision.action == "SELL_SHORT" and current_position.side == "short")
            )
        )
        action_type = gate_action_for_decision(decision, same_side_position=same_side)

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

        instrument = lookup_instrument(decision.inst_id)
        mark_for_existing = (
            current_position.mark_price
            if current_position and current_position.mark_price > 0
            else entry_price
        )
        existing_notional = (
            notional_from_size(existing_size, mark_for_existing, instrument)
            if current_position
            else 0.0
        )

        available_margin: float | None = None
        try:
            available_margin = float(self._exchange.get_balance().available_balance)
        except Exception:
            available_margin = None

        sized = size_order(
            inst_id=decision.inst_id,
            requested_margin=float(decision.margin_usdt),
            leverage=int(decision.leverage),
            entry_price=float(entry_price),
            constraints=SizeConstraints(
                max_margin=float(settings.max_single_asset_margin),
                max_notional=float(settings.effective_max_notional_per_instrument),
                max_contracts=float(settings.effective_max_contracts_per_instrument),
                available_margin=available_margin,
                existing_margin=existing_margin,
                existing_notional=existing_notional,
                existing_size=existing_size,
            ),
            instrument=instrument,
        )
        if sized.error or sized.size <= 0:
            err = sized.error or "Calculated size is zero"
            self._ledger.record_event(
                "order_failed",
                inst_id=decision.inst_id,
                data={"error": err, "action": decision.action},
            )
            return ExecutionResult(
                inst_id=decision.inst_id,
                action=decision.action,
                success=False,
                error=err,
            )

        decision = replace(
            decision,
            margin_usdt=sized.margin_usdt,
            leverage=sized.leverage,
        )
        size = sized.size
        if sized.clipped:
            self._ledger.record_event(
                "order_sized",
                inst_id=decision.inst_id,
                data={
                    "action": decision.action,
                    "margin_usdt": sized.margin_usdt,
                    "notional": sized.notional,
                    "size": sized.size,
                    "leverage": sized.leverage,
                    "clip_notes": list(sized.clip_notes),
                    **provenance_fields(decision),
                },
            )

        # Kill-switch = no real exchange orders. When shadow_mode, skip kill deny
        # so rehearsal can ledger shadow_fill (orchestrator never calls place_order).
        gates_kill = bool(kill_switch) and not bool(shadow_mode)
        ctx = GateContext(
            inst_id=decision.inst_id,
            action=action_type,
            size=size,
            margin_required=sized.margin_usdt,
            current_positions=position_count,
            long_positions=long_count,
            short_positions=short_count,
            daily_pnl=daily_pnl,
            existing_margin_for_asset=existing_margin,
            cooldown_until=cooldown_until,
            kill_switch_active=gates_kill,
            shadow_mode=bool(shadow_mode),
            notional=sized.notional,
            existing_notional_for_asset=existing_notional,
            existing_size_for_asset=existing_size,
        )

        passed, results = check_all_gates(ctx, self._risk_gates)
        if not passed:
            failed_gate = next((r for r in results if not r.passed), None)
            gate_name = failed_gate.gate_name if failed_gate else "unknown"
            reason = failed_gate.reason if failed_gate else "Risk gate failed"
            self._ledger.record_event(
                "risk_gate_blocked",
                inst_id=decision.inst_id,
                data={
                    "gate": gate_name,
                    "error": reason,
                    "action": decision.action,
                    **provenance_fields(decision),
                },
            )
            return ExecutionResult(
                inst_id=decision.inst_id,
                action=decision.action,
                success=False,
                risk_gate_failed=gate_name,
                error=reason,
            )

        if shadow_mode:
            return self._shadow_fill(
                decision=decision,
                entry_price=entry_price,
                size=size,
                had_position=current_position is not None,
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
                leverage=int(decision.leverage),
            )
        )

        return self._finalize_order(
            decision=decision,
            order_result=order_result,
            entry_price=entry_price,
            size=size,
            had_position=current_position is not None,
        )


    def _latest_parent_open_id(self, inst_id: str, direction: str) -> int | None:
        """P1-9: latest ``open`` trade id for inst+direction (parent for scale_in)."""
        finder = getattr(self._ledger, "latest_open_trade_id", None)
        if callable(finder):
            try:
                oid = finder(inst_id, direction)
            except Exception:
                return None
            if oid is None:
                return None
            try:
                return int(oid)
            except (TypeError, ValueError):
                return None
        return None

    def _scale_in_meta(
        self,
        *,
        base: dict,
        had_position: bool,
        direction: str,
        inst_id: str,
    ) -> dict:
        """Stamp parent open_trade_id on scale_in metadata when available."""
        meta = dict(base)
        if had_position:
            parent = self._latest_parent_open_id(inst_id, direction)
            if parent is not None:
                meta["open_trade_id"] = parent
        return meta

    def _confirm_sl_tp_attach(
        self,
        *,
        decision: Decision,
        order_id: str | None,
        size: float,
        entry_price: float,
    ) -> dict:
        """
        P1-7: after fill, query pending algos once → ``sl_tp_attached`` /
        ``sl_tp_attach_failed`` (with algo ids when present).

        Returns a small metadata stamp for the trade row (empty if N/A).
        """
        want_sl = decision.stop_loss is not None and float(decision.stop_loss or 0) > 0
        want_tp = decision.take_profit is not None and float(decision.take_profit or 0) > 0
        if not want_sl and not want_tp:
            return {}
        lister = getattr(self._exchange, "get_pending_oco", None)
        if not callable(lister):
            # Paper / adapters without algo listing — no confirmation event.
            return {}
        try:
            algos = list(lister(decision.inst_id) or [])
        except Exception:
            algos = []
        pos_side = "long" if decision.action == "BUY_LONG" else "short"
        matching: list[dict] = []
        for row in algos:
            if not isinstance(row, dict):
                continue
            inst = str(row.get("instId") or row.get("inst_id") or "")
            if inst and inst != decision.inst_id:
                continue
            side = str(row.get("posSide") or row.get("pos_side") or "").lower()
            if side and side != pos_side:
                continue
            matching.append(row)
        algo_ids = [
            str(a.get("algoId") or a.get("algo_id") or "")
            for a in matching
            if (a.get("algoId") or a.get("algo_id"))
        ]
        algo_ids = [x for x in algo_ids if x]
        payload = {
            "order_id": order_id,
            "action": decision.action,
            "price": entry_price,
            "size": size,
            "take_profit": decision.take_profit,
            "stop_loss": decision.stop_loss,
            "algo_ids": algo_ids,
            "pending_algo_count": len(matching),
            **provenance_fields(decision),
        }
        stamp: dict = {"algo_ids": algo_ids, "pending_algo_count": len(matching)}
        if matching:
            self._ledger.record_event(
                "sl_tp_attached",
                inst_id=decision.inst_id,
                data=payload,
            )
            stamp["sl_tp_attached"] = True
        else:
            self._ledger.record_event(
                "sl_tp_attach_failed",
                inst_id=decision.inst_id,
                data={**payload, "reason": "no_pending_oco"},
            )
            stamp["sl_tp_attach_failed"] = True
            stamp["sl_tp_attach_reason"] = "no_pending_oco"
        return stamp

    def _shadow_fill(
        self,
        *,
        decision: Decision,
        entry_price: float,
        size: float,
        had_position: bool,
    ) -> ExecutionResult:
        """Ledger a shadow fill without calling exchange place_order."""
        order_id = f"shadow-{int(time.time() * 1000)}"
        probe = is_probe_decision(decision)
        prov = provenance_fields(decision)
        event_data = {
            "order_id": order_id,
            "action": decision.action,
            "price": entry_price,
            "size": size,
            "leverage": decision.leverage,
            "margin_usdt": decision.margin_usdt,
            "take_profit": decision.take_profit,
            "stop_loss": decision.stop_loss,
            "confidence": decision.confidence,
            "reason": decision.reason,
            "shadow": True,
            **prov,
        }
        if probe:
            event_data["policy"] = PROBE_POLICY
            event_data["probe"] = True
            event_data.update(probe_audit_fields(decision))
        self._ledger.record_event(
            "shadow_fill",
            inst_id=decision.inst_id,
            data=event_data,
        )
        direction = "long" if decision.action == "BUY_LONG" else "short"
        meta = self._scale_in_meta(
            base={
                "order_id": order_id,
                "leverage": decision.leverage,
                "margin_usdt": decision.margin_usdt,
                "take_profit": decision.take_profit,
                "stop_loss": decision.stop_loss,
                "shadow": True,
                **prov,
            },
            had_position=had_position,
            direction=direction,
            inst_id=decision.inst_id,
        )
        strategy_tag = "keel-shadow"
        if probe:
            meta["policy"] = PROBE_POLICY
            meta["probe"] = True
            meta.update(probe_audit_fields(decision))
            strategy_tag = PROBE_STRATEGY_TAG
        self._ledger.record_trade(
            TradeRecord(
                timestamp=time.time(),
                inst_id=decision.inst_id,
                action="open" if not had_position else "scale_in",
                direction=direction,
                size=size,
                price=entry_price,
                strategy_tag=strategy_tag,
                reason=decision.reason,
                metadata=meta,
            )
        )
        return ExecutionResult(
            inst_id=decision.inst_id,
            action=decision.action,
            success=True,
            order_id=order_id,
            price=entry_price,
            size=size,
            filled=True,
            shadow=True,
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
            prov = provenance_fields(decision)
            direction = "long" if decision.action == "BUY_LONG" else "short"
            meta = self._scale_in_meta(
                base={
                    "order_id": order_result.order_id,
                    "leverage": decision.leverage,
                    "margin_usdt": decision.margin_usdt,
                    "take_profit": decision.take_profit,
                    "stop_loss": decision.stop_loss,
                    **prov,
                },
                had_position=had_position,
                direction=direction,
                inst_id=decision.inst_id,
            )
            attach_stamp = self._confirm_sl_tp_attach(
                decision=decision,
                order_id=order_result.order_id,
                size=size,
                entry_price=entry_price,
            )
            if attach_stamp:
                meta.update(attach_stamp)
            self._ledger.record_trade(
                TradeRecord(
                    timestamp=time.time(),
                    inst_id=decision.inst_id,
                    action="open" if not had_position else "scale_in",
                    direction=direction,
                    size=size,
                    price=entry_price,
                    strategy_tag="keel-llm",
                    reason=decision.reason,
                    metadata=meta,
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
                    "take_profit": decision.take_profit,
                    "stop_loss": decision.stop_loss,
                    **prov,
                    **({k: attach_stamp[k] for k in ("algo_ids", "sl_tp_attached", "sl_tp_attach_failed") if k in attach_stamp}),
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
            data={
                "order_id": order_result.order_id,
                "action": decision.action,
                **provenance_fields(decision),
            },
        )
        return ExecutionResult(
            inst_id=decision.inst_id,
            action=decision.action,
            success=True,
            order_id=order_result.order_id,
            price=entry_price,
            size=size,
        )
