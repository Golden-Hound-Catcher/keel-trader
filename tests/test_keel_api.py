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
from keel.domain import TradeRecord
from keel.domain.instruments import DEFAULT_CRYPTO_INSTRUMENTS
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
        self.assertIn("risk_deny_reasons", lc)
        self.assertIsInstance(lc["risk_deny_reasons"], list)
        self.assertIn("error_count", lc)
        self.assertIsInstance(lc["error_count"], int)
        self.assertIn("errors", lc)
        self.assertIsInstance(lc["errors"], list)
        self.assertIn("duration_ms", lc)
        self.assertIsInstance(lc["duration_ms"], int)
        self.assertGreaterEqual(lc["duration_ms"], 0)

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

    def test_status_seconds_since_last_cycle_with_cycle(self):
        r = self.client.get("/api/v1/status")
        self.assertEqual(r.status_code, 200)
        body = r.json()
        self.assertIn("seconds_since_last_cycle", body)
        lag = body["seconds_since_last_cycle"]
        self.assertIsInstance(lag, int)
        self.assertGreaterEqual(lag, 0)
        self.assertLess(lag, 120)

    def test_ready_with_recent_cycle(self):
        r = self.client.get("/ready")
        self.assertEqual(r.status_code, 200)
        body = r.json()
        self.assertTrue(body["ready"])
        self.assertFalse(body["worker_stale"])
        lag = body["seconds_since_last_cycle"]
        self.assertIsInstance(lag, int)
        self.assertGreaterEqual(lag, 0)
        self.assertLess(lag, 120)
        self.assertIn("okx_configured", body)
        self.assertIn("llm_configured", body)

    def test_config_exposes_non_secret_extensions(self):
        r = self.client.get("/api/v1/config")
        self.assertEqual(r.status_code, 200)
        body = r.json()
        expected = [i.inst_id for i in DEFAULT_CRYPTO_INSTRUMENTS]
        self.assertEqual(body["instruments"], expected)
        self.assertIn("notify_configured", body)
        self.assertIsInstance(body["notify_configured"], bool)
        self.assertIn("exchange_mode", body)
        self.assertIsInstance(body["exchange_mode"], str)
        self.assertTrue(body["exchange_mode"])

    def test_daily_pnl_endpoint(self):
        import time
        from datetime import datetime
        from keel.domain.records import BJ_TZ

        today = datetime.now(BJ_TZ).strftime("%Y-%m-%d")
        self.ledger.record_trade(
            TradeRecord(
                timestamp=time.time(),
                inst_id="BTC-USDT-SWAP",
                action="close",
                direction="long",
                size=1.0,
                price=51000.0,
                pnl=42.5,
            )
        )
        r = self.client.get("/api/v1/pnl/daily")
        self.assertEqual(r.status_code, 200)
        body = r.json()
        self.assertEqual(body["date"], today)
        self.assertEqual(body["source"], "ledger")
        self.assertGreaterEqual(body["realized_pnl"], 42.5)

        r = self.client.get(f"/api/v1/pnl/daily?date={today}")
        self.assertEqual(r.status_code, 200)
        self.assertEqual(r.json()["date"], today)
        self.assertGreaterEqual(r.json()["realized_pnl"], 42.5)


class TestApiStatusWithoutCycle(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.db = Path(self.temp.name) / "empty_status.db"
        set_ledger_path_override(self.db)
        os.environ["KEEL_LEDGER_DB"] = str(self.db)
        refresh_settings()
        self.ledger = KeelLedger(self.db)
        self.app = create_app()
        self.client = TestClient(self.app)

    def tearDown(self):
        self.ledger.close()
        set_ledger_path_override(None)
        os.environ.pop("KEEL_LEDGER_DB", None)
        refresh_settings()
        self.temp.cleanup()

    def test_status_seconds_since_last_cycle_null_without_cycle(self):
        r = self.client.get("/api/v1/status")
        self.assertEqual(r.status_code, 200)
        body = r.json()
        self.assertIsNone(body.get("last_cycle"))
        self.assertIsNone(body.get("seconds_since_last_cycle"))

    def test_ready_cold_start_without_cycle(self):
        """No cycle yet: ready stays True if ledger opens (cold start OK)."""
        r = self.client.get("/ready")
        self.assertEqual(r.status_code, 200)
        body = r.json()
        self.assertTrue(body["ready"])
        self.assertFalse(body["worker_stale"])
        self.assertIsNone(body.get("seconds_since_last_cycle"))

    def test_daily_pnl_zero_without_trades(self):
        from datetime import datetime
        from keel.domain.records import BJ_TZ

        today = datetime.now(BJ_TZ).strftime("%Y-%m-%d")
        r = self.client.get("/api/v1/pnl/daily")
        self.assertEqual(r.status_code, 200)
        body = r.json()
        self.assertEqual(body["date"], today)
        self.assertEqual(body["realized_pnl"], 0.0)
        self.assertEqual(body["source"], "ledger")



class TestApiReadyWorkerLag(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.db = Path(self.temp.name) / "ready_lag.db"
        set_ledger_path_override(self.db)
        os.environ["KEEL_LEDGER_DB"] = str(self.db)
        refresh_settings()
        self.ledger = KeelLedger(self.db)
        self.app = create_app()
        self.client = TestClient(self.app)

    def tearDown(self):
        self.ledger.close()
        set_ledger_path_override(None)
        os.environ.pop("KEEL_LEDGER_DB", None)
        refresh_settings()
        self.temp.cleanup()

    def test_ready_worker_stale_when_cycle_older_than_900s(self):
        import time

        self.ledger.record_cycle_summary(
            {
                "timestamp": time.time() - 950,
                "mode": "paper",
                "adapter": "paper",
                "policy": "rule",
                "instruments": 1,
                "decision_counts": {"WAIT": 1},
                "risk_denies": 0,
                "risk_deny_reasons": [],
                "error_count": 0,
                "errors": [],
                "duration_ms": 1,
            }
        )
        r = self.client.get("/ready")
        self.assertEqual(r.status_code, 200)
        body = r.json()
        self.assertFalse(body["ready"])
        self.assertTrue(body["worker_stale"])
        lag = body["seconds_since_last_cycle"]
        self.assertIsInstance(lag, int)
        self.assertGreater(lag, 900)
        self.assertIn("okx_configured", body)
        self.assertIn("llm_configured", body)

    def test_ready_false_when_ledger_unreadable(self):
        # Point ledger at a directory path so sqlite cannot open it as a DB file.
        bad = Path(self.temp.name) / "not_a_db"
        bad.mkdir()
        set_ledger_path_override(bad)
        os.environ["KEEL_LEDGER_DB"] = str(bad)
        refresh_settings()
        app = create_app()
        client = TestClient(app)
        r = client.get("/ready")
        self.assertEqual(r.status_code, 200)
        body = r.json()
        self.assertFalse(body["ready"])
        self.assertFalse(body["worker_stale"])
        self.assertIsNone(body.get("seconds_since_last_cycle"))
        self.assertIn("okx_configured", body)
        self.assertIn("llm_configured", body)


class TestApiTokenAuth(unittest.TestCase):
    """Optional KEEL_API_TOKEN: 401 without header; 200 with Bearer/X-API-Key; empty → open."""

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.db = Path(self.temp.name) / "token_auth.db"
        set_ledger_path_override(self.db)
        os.environ["KEEL_LEDGER_DB"] = str(self.db)
        self._prev_token = os.environ.get("KEEL_API_TOKEN")

    def tearDown(self):
        set_ledger_path_override(None)
        os.environ.pop("KEEL_LEDGER_DB", None)
        if self._prev_token is None:
            os.environ.pop("KEEL_API_TOKEN", None)
        else:
            os.environ["KEEL_API_TOKEN"] = self._prev_token
        refresh_settings()
        self.temp.cleanup()

    def _client_with_token(self, token: str) -> TestClient:
        if token:
            os.environ["KEEL_API_TOKEN"] = token
        else:
            os.environ.pop("KEEL_API_TOKEN", None)
        refresh_settings()
        KeelLedger(self.db)  # ensure DB exists
        return TestClient(create_app())

    def test_token_set_requires_auth(self):
        client = self._client_with_token("secret-token")
        r = client.get("/api/v1/status")
        self.assertEqual(r.status_code, 401)
        self.assertEqual(r.json().get("detail"), "Unauthorized")

        r = client.get("/health")
        self.assertEqual(r.status_code, 200)

        r = client.get("/api/v1/status", headers={"Authorization": "Bearer secret-token"})
        self.assertEqual(r.status_code, 200)

        r = client.get("/api/v1/config", headers={"X-API-Key": "secret-token"})
        self.assertEqual(r.status_code, 200)

        r = client.get("/api/v1/status", headers={"Authorization": "Bearer wrong"})
        self.assertEqual(r.status_code, 401)

    def test_empty_token_keeps_api_open(self):
        client = self._client_with_token("")
        r = client.get("/api/v1/status")
        self.assertEqual(r.status_code, 200)


if __name__ == "__main__":
    unittest.main()
