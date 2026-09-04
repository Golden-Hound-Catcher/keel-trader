"""Unit tests for typed API response models and OpenAPI honesty."""
from __future__ import annotations

import unittest

from fastapi.testclient import TestClient

from keel.api.app import create_app
from keel.api.schemas import (
    BalanceResponse,
    DecisionItem,
    DecisionsResponse,
    EventItem,
    FactorsResponse,
    HealthResponse,
    MacdBlock,
    PositionsResponse,
    LastCycleSummary,
    StatusResponse,
    TradesResponse,
)
from keel.domain import Decision, DecisionRecord, FactorSnapshot, LedgerEvent, TradeRecord


class TestApiSchemas(unittest.TestCase):
    def test_last_cycle_summary_model(self):
        m = LastCycleSummary(
            timestamp=1.0,
            mode="paper",
            adapter="paper",
            policy="rule",
            instruments=1,
            decision_counts={"WAIT": 1},
            risk_denies=0,
            errors=[],
        )
        self.assertEqual(m.mode, "paper")
        status = StatusResponse(
            version="0.1.0",
            mode="read_only_control_plane",
            uptime_seconds=1,
            environment="demo",
            credentials={"okx": False, "llm": False},
            ledger_db="/tmp/x.db",
            last_cycle=m,
        )
        self.assertEqual(status.last_cycle.instruments, 1)

    def test_health_response_model(self):
        m = HealthResponse(
            status="ok",
            service="keel-trader",
            version="0.1.0",
            timestamp=1,
            environment="demo",
        )
        self.assertEqual(m.model_dump()["status"], "ok")

    def test_decision_item_from_record(self):
        rec = DecisionRecord(
            id=1,
            timestamp=100.0,
            inst_id="BTC-USDT-SWAP",
            action="WAIT",
            confidence=10.0,
        )
        item = DecisionItem(
            id=rec.id,
            timestamp=rec.timestamp,
            inst_id=rec.inst_id,
            action=rec.action,
            confidence=rec.confidence,
        )
        self.assertEqual(item.inst_id, "BTC-USDT-SWAP")

    def test_domain_decision_shared(self):
        d = Decision(inst_id="ETH-USDT-SWAP", action="WAIT")
        self.assertTrue(d.valid)

    def test_ledger_event_shape(self):
        e = LedgerEvent(id=1, timestamp=1.0, event_type="paper_cycle_complete", data={"ok": True})
        item = EventItem(
            id=e.id,
            timestamp=e.timestamp,
            event_type=e.event_type,
            inst_id=e.inst_id,
            data=e.data,
        )
        self.assertEqual(item.event_type, "paper_cycle_complete")

    def test_factors_response_requires_macd(self):
        fr = FactorsResponse(
            inst_id="BTC-USDT-SWAP",
            source="ledger",
            price=100.0,
            ema_9=99.0,
            ema_21=98.0,
            rsi_14=55.0,
            atr_14=1.0,
            macd=MacdBlock(line=0.1, signal=0.05, histogram=0.05),
        )
        self.assertEqual(fr.source, "ledger")

    def test_openapi_includes_typed_paths(self):
        client = TestClient(create_app())
        schema = client.get("/openapi.json").json()
        paths = schema["paths"]
        self.assertIn("/health", paths)
        self.assertIn("/api/v1/status", paths)
        self.assertIn("/api/v1/positions", paths)
        self.assertIn("/api/v1/balance", paths)
        self.assertIn("/api/v1/decisions", paths)
        self.assertIn("/api/v1/trades", paths)
        self.assertIn("/api/v1/events", paths)
        self.assertIn("/api/v1/factors/{inst_id}", paths)
        # response_model registers component schemas
        comps = schema.get("components", {}).get("schemas", {})
        self.assertIn("HealthResponse", comps)
        self.assertIn("StatusResponse", comps)
        self.assertIn("LastCycleSummary", comps)
        self.assertIn("DecisionsResponse", comps)
        self.assertIn("FactorsResponse", comps)
        status_props = comps["StatusResponse"]["properties"]
        self.assertIn("last_cycle", status_props)


class TestDomainRecordsExports(unittest.TestCase):
    def test_trade_and_factor_snapshot(self):
        t = TradeRecord(inst_id="BTC-USDT-SWAP", action="open", direction="long", size=1, price=100)
        self.assertEqual(t.action, "open")
        s = FactorSnapshot(inst_id="BTC-USDT-SWAP", price=100.0, rsi_14=50.0)
        self.assertEqual(s.trend_15m, "neutral")


if __name__ == "__main__":
    unittest.main()
