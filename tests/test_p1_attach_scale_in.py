"""P1-7 SL/TP attach confirmation + P1-9 scale_in parent open_trade_id."""
from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from typing import Any

from keel.domain.decision import Decision
from keel.domain.records import TradeRecord
from keel.exchange.paper import PaperAdapter
from keel.exchange.protocol import Order, OrderResult, Ticker
from keel.execution.orchestrator import ExecutionOrchestrator
from keel.ledger import KeelLedger


class _AlgoExchange:
    """Minimal exchange stub with place_order + get_pending_oco."""

    def __init__(self, *, algos: list[dict[str, Any]] | None = None) -> None:
        self._algos = list(algos or [])
        self._ticker = Ticker(
            inst_id="BTC-USDT-SWAP",
            last=100.0,
            bid=99.9,
            ask=100.1,
        )

    def get_ticker(self, inst_id: str) -> Ticker:
        return self._ticker

    def get_positions(self):
        return []

    def get_balance(self):
        from keel.exchange.protocol import AccountBalance

        return AccountBalance(
            total_equity=10_000.0,
            available_balance=10_000.0,
            cash_balance=10_000.0,
            unrealized_pnl=0.0,
            margin_used=0.0,
        )

    def place_order(self, request) -> OrderResult:
        return OrderResult(
            success=True,
            order_id="ord-1",
            order=Order(
                order_id="ord-1",
                inst_id=request.inst_id,
                side=request.side,
                pos_side=request.pos_side,
                order_type="limit",
                size=request.size,
                filled_size=request.size,
                price=request.price or 100.0,
                state="filled",
            ),
        )

    def get_pending_oco(self, inst_id: str | None = None) -> list[dict[str, Any]]:
        if inst_id:
            return [
                a
                for a in self._algos
                if str(a.get("instId") or a.get("inst_id") or "") in ("", inst_id)
            ]
        return list(self._algos)


class TestSlTpAttach(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.db = Path(self.temp.name) / "attach.db"
        self.ledger = KeelLedger(self.db)

    def tearDown(self) -> None:
        self.ledger.close()
        self.temp.cleanup()

    def _decision(self) -> Decision:
        return Decision(
            inst_id="BTC-USDT-SWAP",
            action="BUY_LONG",
            confidence=80,
            entry_price=100.2,
            take_profit=122.0,
            stop_loss=90.0,
            leverage=3,
            margin_usdt=50.0,
            reason="attach unit",
        )

    def _filled_result(self) -> OrderResult:
        return OrderResult(
            success=True,
            order_id="ord-1",
            order=Order(
                order_id="ord-1",
                inst_id="BTC-USDT-SWAP",
                side="buy",
                pos_side="long",
                order_type="limit",
                size=0.1,
                filled_size=0.1,
                price=100.2,
                state="filled",
            ),
        )

    def test_sl_tp_attached_when_pending_oco(self) -> None:
        ex = _AlgoExchange(
            algos=[
                {
                    "instId": "BTC-USDT-SWAP",
                    "posSide": "long",
                    "algoId": "algo-99",
                }
            ]
        )
        orch = ExecutionOrchestrator(exchange=ex, ledger=self.ledger)  # type: ignore[arg-type]
        result = orch._finalize_order(
            decision=self._decision(),
            order_result=self._filled_result(),
            entry_price=100.2,
            size=0.1,
            had_position=False,
        )
        self.assertTrue(result.filled)
        attached = self.ledger.get_events(event_type="sl_tp_attached")
        self.assertEqual(len(attached), 1)
        payload = attached[0].data or {}
        self.assertIn("algo-99", payload.get("algo_ids") or [])
        self.assertEqual(len(self.ledger.get_events(event_type="sl_tp_attach_failed")), 0)
        trade = self.ledger.get_trades(limit=1)[0]
        self.assertTrue((trade.metadata or {}).get("sl_tp_attached"))
        self.assertIn("algo-99", (trade.metadata or {}).get("algo_ids") or [])

    def test_sl_tp_attach_failed_when_no_pending(self) -> None:
        ex = _AlgoExchange(algos=[])
        orch = ExecutionOrchestrator(exchange=ex, ledger=self.ledger)  # type: ignore[arg-type]
        result = orch._finalize_order(
            decision=self._decision(),
            order_result=self._filled_result(),
            entry_price=100.2,
            size=0.1,
            had_position=False,
        )
        self.assertTrue(result.filled)
        failed = self.ledger.get_events(event_type="sl_tp_attach_failed")
        self.assertEqual(len(failed), 1)
        self.assertEqual((failed[0].data or {}).get("reason"), "no_pending_oco")
        self.assertEqual(len(self.ledger.get_events(event_type="sl_tp_attached")), 0)

    def test_paper_adapter_skips_attach_events(self) -> None:
        paper = PaperAdapter(initial_balance=10_000.0)
        paper.set_ticker(
            Ticker(inst_id="BTC-USDT-SWAP", last=100.0, bid=99.9, ask=100.1)
        )
        orch = ExecutionOrchestrator(exchange=paper, ledger=self.ledger)
        result = orch._finalize_order(
            decision=self._decision(),
            order_result=self._filled_result(),
            entry_price=100.2,
            size=0.1,
            had_position=False,
        )
        self.assertTrue(result.filled)
        self.assertEqual(len(self.ledger.get_events(event_type="sl_tp_attached")), 0)
        self.assertEqual(len(self.ledger.get_events(event_type="sl_tp_attach_failed")), 0)


class TestScaleInParent(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.db = Path(self.temp.name) / "scale.db"
        self.ledger = KeelLedger(self.db)

    def tearDown(self) -> None:
        self.ledger.close()
        self.temp.cleanup()

    def test_latest_open_trade_id(self) -> None:
        self.ledger.record_trade(
            TradeRecord(
                timestamp=1.0,
                inst_id="BTC-USDT-SWAP",
                action="open",
                direction="long",
                size=1.0,
                price=100.0,
            )
        )
        t2 = self.ledger.record_trade(
            TradeRecord(
                timestamp=2.0,
                inst_id="BTC-USDT-SWAP",
                action="open",
                direction="long",
                size=1.0,
                price=101.0,
            )
        )
        self.assertEqual(self.ledger.latest_open_trade_id("BTC-USDT-SWAP", "long"), t2)
        self.assertIsNone(self.ledger.latest_open_trade_id("BTC-USDT-SWAP", "short"))

    def test_shadow_scale_in_stamps_parent(self) -> None:
        paper = PaperAdapter(initial_balance=10_000.0)
        paper.set_ticker(
            Ticker(inst_id="BTC-USDT-SWAP", last=100.0, bid=99.9, ask=100.1)
        )
        orch = ExecutionOrchestrator(exchange=paper, ledger=self.ledger)
        parent_id = self.ledger.record_trade(
            TradeRecord(
                timestamp=1.0,
                inst_id="BTC-USDT-SWAP",
                action="open",
                direction="long",
                size=1.0,
                price=100.0,
                strategy_tag="keel-shadow",
                metadata={"shadow": True},
            )
        )
        decision = Decision(
            inst_id="BTC-USDT-SWAP",
            action="BUY_LONG",
            confidence=80,
            entry_price=100.2,
            take_profit=122.0,
            stop_loss=90.0,
            leverage=3,
            margin_usdt=50.0,
            reason="scale parent",
        )
        result = orch._shadow_fill(
            decision=decision,
            entry_price=100.2,
            size=0.1,
            had_position=True,
        )
        self.assertTrue(result.success)
        trades = self.ledger.get_trades(limit=5)
        scale = next(t for t in trades if t.action == "scale_in")
        self.assertEqual((scale.metadata or {}).get("open_trade_id"), parent_id)


class TestDecisionStatsP1(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.db = Path(self.temp.name) / "stats.db"
        self.ledger = KeelLedger(self.db)

    def tearDown(self) -> None:
        self.ledger.close()
        self.temp.cleanup()

    def test_invalid_and_deny_histogram(self) -> None:
        import time

        now = time.time()
        self.ledger.record_event(
            "decision_invalid",
            inst_id="BTC-USDT-SWAP",
            data={"error": "timeout"},
            timestamp=now,
        )
        self.ledger.record_event(
            "risk_gate_blocked",
            inst_id="BTC-USDT-SWAP",
            data={"gate": "max_notional"},
            timestamp=now,
        )
        self.ledger.record_event(
            "risk_gate_blocked",
            inst_id="ETH-USDT-SWAP",
            data={"gate": "max_notional"},
            timestamp=now,
        )
        self.ledger.record_event(
            "risk_gate_blocked",
            inst_id="SOL-USDT-SWAP",
            data={"gate": "daily_loss"},
            timestamp=now,
        )
        stats = self.ledger.get_decision_stats(hours=24.0)
        self.assertEqual(stats["decision_invalid_events"], 1)
        self.assertEqual(stats["risk_deny_events"], 3)
        self.assertEqual(stats["risk_deny_by_gate"].get("max_notional"), 2)
        self.assertEqual(stats["risk_deny_by_gate"].get("daily_loss"), 1)


if __name__ == "__main__":
    unittest.main()
