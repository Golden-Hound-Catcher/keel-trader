"""Tests for keel.ledger module - SQLite ledger."""
import os
import tempfile
import time
import unittest
from pathlib import Path

from keel.ledger.sqlite_ledger import KeelLedger, TradeRecord, DecisionRecord


class TestKeelLedger(unittest.TestCase):
    """Tests for the SQLite ledger."""

    def setUp(self):
        self.temp_dir = tempfile.mkdtemp()
        self.db_path = Path(self.temp_dir) / "test_ledger.db"
        self.ledger = KeelLedger(self.db_path)

    def tearDown(self):
        self.ledger.close()
        if self.db_path.exists():
            os.unlink(self.db_path)
        os.rmdir(self.temp_dir)

    def test_record_trade(self):
        trade = TradeRecord(
            timestamp=time.time(),
            inst_id="BTC-USDT-SWAP",
            action="open",
            direction="long",
            size=1.0,
            price=50000.0,
            strategy_tag="test",
            reason="unit test",
        )
        trade_id = self.ledger.record_trade(trade)
        self.assertGreater(trade_id, 0)

    def test_get_trades(self):
        for i in range(5):
            trade = TradeRecord(
                timestamp=time.time(),
                inst_id=f"TEST{i}-USDT-SWAP",
                action="open",
                direction="long",
                size=float(i + 1),
                price=100.0 * (i + 1),
            )
            self.ledger.record_trade(trade)

        trades = self.ledger.get_trades(limit=10)
        self.assertEqual(len(trades), 5)
        self.assertGreater(trades[0].size, trades[-1].size)

    def test_get_trades_filtered(self):
        self.ledger.record_trade(TradeRecord(
            timestamp=time.time(),
            inst_id="BTC-USDT-SWAP",
            action="open",
            direction="long",
            size=1.0,
            price=50000.0,
        ))
        self.ledger.record_trade(TradeRecord(
            timestamp=time.time(),
            inst_id="ETH-USDT-SWAP",
            action="open",
            direction="long",
            size=1.0,
            price=3000.0,
        ))

        btc_trades = self.ledger.get_trades(inst_id="BTC-USDT-SWAP")
        self.assertEqual(len(btc_trades), 1)
        self.assertEqual(btc_trades[0].inst_id, "BTC-USDT-SWAP")

    def test_record_decision(self):
        decision = DecisionRecord(
            timestamp=time.time(),
            inst_id="BTC-USDT-SWAP",
            action="BUY_LONG",
            confidence=85.0,
            entry_price=50000.0,
            take_profit=55000.0,
            stop_loss=48000.0,
            reason="Strong trend",
        )
        decision_id = self.ledger.record_decision(decision)
        self.assertGreater(decision_id, 0)

    def test_get_latest_decision_fresh(self):
        decision = DecisionRecord(
            timestamp=time.time(),
            inst_id="BTC-USDT-SWAP",
            action="WAIT",
            confidence=0.0,
            reason="No signal",
        )
        self.ledger.record_decision(decision)

        latest = self.ledger.get_latest_decision("BTC-USDT-SWAP", max_age_seconds=300)
        self.assertIsNotNone(latest)
        self.assertEqual(latest.action, "WAIT")

    def test_get_latest_decision_stale(self):
        decision = DecisionRecord(
            timestamp=time.time() - 400,
            inst_id="BTC-USDT-SWAP",
            action="WAIT",
            confidence=0.0,
            reason="Old decision",
        )
        self.ledger.record_decision(decision)

        latest = self.ledger.get_latest_decision("BTC-USDT-SWAP", max_age_seconds=300)
        self.assertIsNone(latest)

    def test_get_daily_pnl(self):
        now = time.time()
        self.ledger.record_trade(TradeRecord(
            timestamp=now,
            inst_id="BTC-USDT-SWAP",
            action="close",
            direction="long",
            size=1.0,
            price=51000.0,
            pnl=100.0,
        ))
        self.ledger.record_trade(TradeRecord(
            timestamp=now,
            inst_id="ETH-USDT-SWAP",
            action="close",
            direction="short",
            size=1.0,
            price=2900.0,
            pnl=-50.0,
        ))

        daily_pnl = self.ledger.get_daily_pnl()
        self.assertEqual(daily_pnl, 50.0)

    def test_record_event(self):
        event_id = self.ledger.record_event(
            event_type="risk_gate_blocked",
            inst_id="BTC-USDT-SWAP",
            data={"gate": "daily_loss", "reason": "Limit exceeded"},
        )
        self.assertGreater(event_id, 0)

    def test_cycle_summary_roundtrip(self):
        self.assertIsNone(self.ledger.get_last_cycle_summary())
        payload = {
            "timestamp": 1_700_000_000.0,
            "mode": "paper",
            "adapter": "paper",
            "policy": "rule",
            "instruments": 2,
            "decision_counts": {"WAIT": 1, "BUY_LONG": 1},
            "risk_denies": 1,
            "risk_deny_reasons": [{"gate": "daily_loss", "reason": "Limit exceeded"}],
            "errors": [],
            "policy_success": True,
            "duration_ms": 17,
        }
        eid = self.ledger.record_cycle_summary(payload)
        self.assertGreater(eid, 0)
        got = self.ledger.get_last_cycle_summary()
        self.assertIsNotNone(got)
        self.assertEqual(got["mode"], "paper")
        self.assertEqual(got["decision_counts"]["BUY_LONG"], 1)
        self.assertEqual(got["timestamp"], 1_700_000_000.0)
        self.assertEqual(got["duration_ms"], 17)
        self.assertEqual(got["risk_denies"], 1)
        self.assertEqual(got["risk_deny_reasons"][0]["gate"], "daily_loss")

    def test_trade_time_str(self):
        trade = TradeRecord(
            timestamp=1704067200.0,
            inst_id="BTC-USDT-SWAP",
            action="open",
            direction="long",
            size=1.0,
            price=50000.0,
        )
        self.assertIn("2024", trade.time_str)


class TestTradeRecord(unittest.TestCase):
    """Tests for TradeRecord dataclass."""

    def test_immutable_after_creation(self):
        trade = TradeRecord(
            inst_id="BTC-USDT-SWAP",
            action="open",
            direction="long",
            size=1.0,
            price=50000.0,
        )
        self.assertEqual(trade.inst_id, "BTC-USDT-SWAP")


class TestDecisionRecord(unittest.TestCase):
    """Tests for DecisionRecord dataclass."""

    def test_creation(self):
        decision = DecisionRecord(
            inst_id="BTC-USDT-SWAP",
            action="BUY_LONG",
            confidence=85.0,
        )
        self.assertEqual(decision.confidence, 85.0)


if __name__ == "__main__":
    unittest.main()
