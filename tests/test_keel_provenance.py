"""History provenance: decision_id / market_source stamped onto fills + API."""
from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from keel.api.routers.decisions import _decision_item, _trade_item
from keel.domain.decision import Decision
from keel.domain.records import DecisionRecord, TradeRecord
from keel.execution.orchestrator import ExecutionOrchestrator
from keel.execution.provenance import provenance_fields
from keel.ledger import KeelLedger


class _FakeTicker:
    bid = 100.0
    ask = 100.1


class _FakeBalance:
    available_balance = 10_000.0


class _FakeExchange:
    def get_positions(self):
        return []

    def get_ticker(self, inst_id: str):
        return _FakeTicker()

    def get_balance(self):
        return _FakeBalance()

    def place_order(self, *args, **kwargs):
        raise AssertionError("shadow path must not place_order")


class TestProvenanceStamp(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.db = Path(self.tmp.name) / "t.db"
        self.ledger = KeelLedger(self.db)

    def tearDown(self) -> None:
        self.tmp.cleanup()

    def test_provenance_fields_compact(self):
        d = Decision(
            inst_id="BTC-USDT-SWAP",
            action="BUY_LONG",
            ledger_id=42,
            market_source="okx_public",
            signal_diag={"rule_variant": "trend_follow"},
        )
        fields = provenance_fields(d, policy_name="rule")
        self.assertEqual(fields["decision_id"], 42)
        self.assertEqual(fields["market_source"], "okx_public")
        self.assertEqual(fields["rule_variant"], "trend_follow")
        self.assertEqual(fields["policy_name"], "rule")

    def test_shadow_fill_stamps_decision_id(self):
        orch = ExecutionOrchestrator(exchange=_FakeExchange(), ledger=self.ledger)
        decision = Decision(
            inst_id="BTC-USDT-SWAP",
            action="BUY_LONG",
            confidence=70.0,
            entry_price=100.0,
            take_profit=102.2,
            stop_loss=99.0,
            leverage=3,
            margin_usdt=50.0,
            reason="rule long trend_follow",
            ledger_id=99,
            market_source="okx_public",
            signal_diag={"rule_variant": "trend_follow", "missing": []},
        )
        result = orch.execute_decision(
            decision,
            daily_pnl=0.0,
            kill_switch=True,
            shadow_mode=True,
        )
        self.assertTrue(result.success)
        self.assertTrue(result.shadow)
        trades = self.ledger.get_trades(limit=5)
        self.assertEqual(len(trades), 1)
        meta = trades[0].metadata or {}
        self.assertEqual(meta.get("decision_id"), 99)
        self.assertEqual(meta.get("market_source"), "okx_public")
        self.assertEqual(meta.get("rule_variant"), "trend_follow")
        events = self.ledger.get_events(event_type="shadow_fill", limit=5)
        self.assertEqual(len(events), 1)
        data = events[0].data or {}
        self.assertEqual(data.get("decision_id"), 99)
        self.assertEqual(data.get("market_source"), "okx_public")

    def test_decision_item_promotes_market_source(self):
        rec = DecisionRecord(
            id=7,
            timestamp=1.0,
            inst_id="ETH-USDT-SWAP",
            action="WAIT",
            confidence=40.0,
            policy_name="rule",
            calculus_data={
                "market_source": "okx_public",
                "data_quality_reason": "okx_public",
                "signal_diag": {"rule_variant": "trend_follow", "nearest": "long"},
            },
        )
        item = _decision_item(rec)
        self.assertEqual(item.market_source, "okx_public")
        self.assertEqual(item.rule_variant, "trend_follow")
        self.assertEqual(item.data_quality_reason, "okx_public")

    def test_trade_item_promotes_metadata(self):
        rec = TradeRecord(
            id=3,
            timestamp=1.0,
            inst_id="SOL-USDT-SWAP",
            action="open",
            direction="short",
            size=1.0,
            price=99.0,
            strategy_tag="keel-shadow",
            reason="rule short",
            metadata={
                "decision_id": 55,
                "market_source": "synthetic",
                "rule_variant": "mean_revert",
                "shadow": True,
                "order_id": "shadow-1",
            },
        )
        item = _trade_item(rec)
        self.assertEqual(item.decision_id, 55)
        self.assertEqual(item.market_source, "synthetic")
        self.assertEqual(item.rule_variant, "mean_revert")
        self.assertTrue(item.shadow)
        self.assertEqual(item.order_id, "shadow-1")


if __name__ == "__main__":
    unittest.main()
