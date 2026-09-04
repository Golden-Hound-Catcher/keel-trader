"""Stage 4: Decision schema → risk → ledger execution coherence."""
from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from keel.exchange.paper import PaperAdapter
from keel.exchange.protocol import Ticker
from keel.execution.orchestrator import ExecutionOrchestrator
from keel.ledger import KeelLedger
from keel.domain.decision import Decision, validate_decision
from keel.risk.gates import KillSwitchGate


class TestValidateDecision(unittest.TestCase):
    def test_valid_long_passes(self):
        d = validate_decision(
            Decision(
                inst_id="BTC-USDT-SWAP",
                action="BUY_LONG",
                entry_price=100.0,
                stop_loss=90.0,
                take_profit=130.0,
                margin_usdt=50,
            )
        )
        self.assertTrue(d.valid)
        self.assertEqual(d.action, "BUY_LONG")

    def test_bad_rr_rewrites_to_wait(self):
        d = validate_decision(
            Decision(
                inst_id="BTC-USDT-SWAP",
                action="BUY_LONG",
                entry_price=100.0,
                stop_loss=90.0,
                take_profit=105.0,  # RR 0.5
                margin_usdt=50,
            )
        )
        self.assertFalse(d.valid)
        self.assertEqual(d.action, "WAIT")
        self.assertIn("Risk:reward", d.validation_error)

    def test_bad_geometry_short(self):
        d = validate_decision(
            Decision(
                inst_id="BTC-USDT-SWAP",
                action="SELL_SHORT",
                entry_price=100.0,
                stop_loss=90.0,  # wrong side
                take_profit=80.0,
                margin_usdt=50,
            )
        )
        self.assertFalse(d.valid)
        self.assertEqual(d.action, "WAIT")


class TestOrchestratorLedgerCoherence(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.db = Path(self.temp.name) / "exec.db"
        self.ledger = KeelLedger(self.db)
        self.exchange = PaperAdapter(initial_balance=10_000.0)
        self.exchange.set_ticker(
            Ticker(
                inst_id="BTC-USDT-SWAP",
                last=100.0,
                bid=99.9,
                ask=100.1,
            )
        )
        self.orch = ExecutionOrchestrator(exchange=self.exchange, ledger=self.ledger)

    def tearDown(self):
        self.ledger.close()
        self.temp.cleanup()

    def test_fill_records_trade_and_event(self):
        decision = Decision(
            inst_id="BTC-USDT-SWAP",
            action="BUY_LONG",
            confidence=80,
            entry_price=100.2,  # >= ask → fill
            take_profit=122.0,
            stop_loss=90.0,
            leverage=3,
            margin_usdt=50.0,
            reason="unit fill",
        )
        result = self.orch.execute_decision(decision)
        self.assertTrue(result.success)
        self.assertTrue(result.filled)
        self.assertEqual(len(self.ledger.get_trades()), 1)
        events = self.ledger.get_events(event_type="order_filled")
        self.assertGreaterEqual(len(events), 1)

    def test_resting_limit_no_trade(self):
        decision = Decision(
            inst_id="BTC-USDT-SWAP",
            action="BUY_LONG",
            confidence=80,
            entry_price=99.0,  # < ask → resting
            take_profit=122.0,
            stop_loss=90.0,
            leverage=3,
            margin_usdt=50.0,
            reason="unit resting",
        )
        result = self.orch.execute_decision(decision)
        self.assertTrue(result.success)
        self.assertTrue(result.resting)
        self.assertEqual(len(self.ledger.get_trades()), 0)
        self.assertGreaterEqual(len(self.ledger.get_events(event_type="order_resting")), 1)

    def test_invalid_decision_event(self):
        decision = Decision(
            inst_id="BTC-USDT-SWAP",
            action="BUY_LONG",
            entry_price=100.0,
            take_profit=101.0,
            stop_loss=99.0,
            margin_usdt=50.0,
        )
        result = self.orch.execute_decision(decision)
        self.assertFalse(result.success)
        self.assertEqual(result.action, "WAIT")
        self.assertGreaterEqual(len(self.ledger.get_events(event_type="decision_invalid")), 1)

    def test_kill_switch_blocks_with_event(self):
        orch = ExecutionOrchestrator(
            exchange=self.exchange,
            ledger=self.ledger,
            risk_gates=[KillSwitchGate()],
        )
        decision = Decision(
            inst_id="BTC-USDT-SWAP",
            action="BUY_LONG",
            entry_price=100.2,
            take_profit=122.0,
            stop_loss=90.0,
            margin_usdt=50.0,
        )
        result = orch.execute_decision(decision, kill_switch=True)
        self.assertFalse(result.success)
        self.assertEqual(result.risk_gate_failed, "kill_switch")
        self.assertGreaterEqual(len(self.ledger.get_events(event_type="risk_gate_blocked")), 1)

    def test_wait_ok_when_kill_switch(self):
        orch = ExecutionOrchestrator(
            exchange=self.exchange,
            ledger=self.ledger,
            risk_gates=[KillSwitchGate()],
        )
        decision = Decision(inst_id="BTC-USDT-SWAP", action="WAIT", reason="idle")
        result = orch.execute_decision(decision, kill_switch=True)
        self.assertTrue(result.success)
        self.assertEqual(result.action, "WAIT")
        self.assertIsNone(result.risk_gate_failed)

    def test_kill_switch_from_settings(self):
        import os
        from keel.config import refresh_settings

        prev = os.environ.get("KEEL_KILL_SWITCH")
        os.environ["KEEL_KILL_SWITCH"] = "1"
        refresh_settings()
        try:
            decision = Decision(
                inst_id="BTC-USDT-SWAP",
                action="BUY_LONG",
                entry_price=100.2,
                take_profit=122.0,
                stop_loss=90.0,
                margin_usdt=50.0,
            )
            result = self.orch.execute_decision(decision)
            self.assertFalse(result.success)
            self.assertEqual(result.risk_gate_failed, "kill_switch")
        finally:
            if prev is None:
                os.environ.pop("KEEL_KILL_SWITCH", None)
            else:
                os.environ["KEEL_KILL_SWITCH"] = prev
            refresh_settings()


if __name__ == "__main__":
    unittest.main()
