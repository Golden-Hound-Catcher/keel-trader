"""Stage 4: keel.api reads ledger after a worker paper cycle."""
from __future__ import annotations

import os
import tempfile
import unittest
from pathlib import Path

from fastapi.testclient import TestClient

from keel.api.app import create_app
from keel.api.deps import set_ledger_path_override
from keel.config import refresh_settings
from keel.exchange.paper import PaperAdapter
from keel.ledger import KeelLedger
from keel.worker.cycle import run_paper_cycle


class TestApiAfterPaperCycle(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.db = Path(self.temp.name) / "api_cycle.db"
        set_ledger_path_override(self.db)
        os.environ["KEEL_LEDGER_DB"] = str(self.db)
        refresh_settings()

        self.ledger = KeelLedger(self.db)
        self.exchange = PaperAdapter(initial_balance=10_000.0)
        summary = run_paper_cycle(
            exchange=self.exchange,
            ledger=self.ledger,
            instrument_ids=["BTC-USDT-SWAP", "ETH-USDT-SWAP"],
            force_action="BUY_LONG",
        )
        self.assertTrue(summary["ok"])
        self.app = create_app()
        self.client = TestClient(self.app)

    def tearDown(self):
        self.ledger.close()
        set_ledger_path_override(None)
        os.environ.pop("KEEL_LEDGER_DB", None)
        refresh_settings()
        self.temp.cleanup()

    def test_health(self):
        r = self.client.get("/health")
        self.assertEqual(r.status_code, 200)
        self.assertEqual(r.json()["status"], "ok")

    def test_decisions_from_ledger(self):
        r = self.client.get("/api/v1/decisions")
        self.assertEqual(r.status_code, 200)
        body = r.json()
        self.assertGreaterEqual(body["count"], 2)
        actions = {d["action"] for d in body["decisions"]}
        self.assertIn("BUY_LONG", actions)

    def test_latest_decision(self):
        r = self.client.get("/api/v1/decisions/latest/BTC-USDT-SWAP")
        self.assertEqual(r.status_code, 200)
        body = r.json()
        self.assertTrue(body["found"])
        self.assertEqual(body["decision"]["inst_id"], "BTC-USDT-SWAP")

    def test_trades_from_ledger(self):
        r = self.client.get("/api/v1/trades")
        self.assertEqual(r.status_code, 200)
        body = r.json()
        self.assertGreaterEqual(body["count"], 1)
        self.assertEqual(body["trades"][0]["inst_id"], "BTC-USDT-SWAP")

    def test_events_include_cycle_complete(self):
        r = self.client.get("/api/v1/events")
        self.assertEqual(r.status_code, 200)
        body = r.json()
        types = {e["event_type"] for e in body["events"]}
        self.assertIn("paper_cycle_complete", types)
        self.assertIn("order_filled", types)

    def test_factors_from_ledger_snapshot(self):
        r = self.client.get("/api/v1/factors/BTC-USDT-SWAP")
        self.assertEqual(r.status_code, 200)
        body = r.json()
        self.assertEqual(body["source"], "ledger")
        self.assertGreater(body["price"], 0)
        self.assertIn("rsi_14", body)

    def test_status_shows_ledger_path(self):
        r = self.client.get("/api/v1/status")
        self.assertEqual(r.status_code, 200)
        self.assertEqual(Path(r.json()["ledger_db"]).resolve(), self.db.resolve())

    def test_status_includes_last_cycle(self):
        r = self.client.get("/api/v1/status")
        self.assertEqual(r.status_code, 200)
        body = r.json()
        lc = body.get("last_cycle")
        self.assertIsInstance(lc, dict)
        self.assertIn("timestamp", lc)
        self.assertEqual(lc["mode"], "paper")
        self.assertGreaterEqual(lc["instruments"], 2)
        self.assertIn("decision_counts", lc)
        self.assertIsInstance(lc["decision_counts"], dict)
        self.assertIn("BUY_LONG", lc["decision_counts"])
        self.assertIn("risk_denies", lc)
        self.assertIn("errors", lc)
        self.assertIsInstance(lc["errors"], list)

    def test_status_and_config_expose_kill_switch(self):
        r = self.client.get("/api/v1/status")
        self.assertEqual(r.status_code, 200)
        self.assertIn("kill_switch", r.json())
        self.assertIsInstance(r.json()["kill_switch"], bool)
        self.assertFalse(r.json()["kill_switch"])

        r = self.client.get("/api/v1/config")
        self.assertEqual(r.status_code, 200)
        self.assertIn("kill_switch", r.json())
        self.assertFalse(r.json()["kill_switch"])

    def test_status_kill_switch_on_when_armed(self):
        os.environ["KEEL_KILL_SWITCH"] = "1"
        try:
            refresh_settings()
            app = create_app()
            client = TestClient(app)
            r = client.get("/api/v1/status")
            self.assertEqual(r.status_code, 200)
            self.assertTrue(r.json()["kill_switch"])
            r = client.get("/api/v1/config")
            self.assertEqual(r.status_code, 200)
            self.assertTrue(r.json()["kill_switch"])
        finally:
            os.environ.pop("KEEL_KILL_SWITCH", None)
            refresh_settings()


if __name__ == "__main__":
    unittest.main()
