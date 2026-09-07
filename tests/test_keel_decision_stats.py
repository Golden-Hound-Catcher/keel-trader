"""P2: decision audit fields, stats API, policy compare script."""
from __future__ import annotations

import json
import os
import sqlite3
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path

from fastapi.testclient import TestClient

from keel.api.app import create_app
from keel.api.deps import set_ledger_path_override
from keel.config import refresh_settings
from keel.domain.records import DecisionRecord
from keel.exchange.paper import PaperAdapter
from keel.ledger import KeelLedger
from keel.policy import StubDecisionPolicy, RuleDecisionPolicy
from keel.worker.cycle import run_paper_cycle

REPO_ROOT = Path(__file__).resolve().parents[1]


class TestDecisionAuditFields(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.db = Path(self.temp.name) / "audit.db"
        self.ledger = KeelLedger(self.db)

    def tearDown(self):
        self.ledger.close()
        self.temp.cleanup()

    def test_record_and_read_policy_modules(self):
        did = self.ledger.record_decision(
            DecisionRecord(
                timestamp=time.time(),
                inst_id="BTC-USDT-SWAP",
                action="WAIT",
                confidence=10.0,
                reason="test",
                policy_name="rule",
                prompt_modules=["system_role.v1", "user_market.v1"],
            )
        )
        self.assertGreater(did, 0)
        rows = self.ledger.get_decisions(limit=5)
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0].policy_name, "rule")
        self.assertEqual(rows[0].prompt_modules, ["system_role.v1", "user_market.v1"])

    def test_migrate_old_db_missing_columns(self):
        """Existing DB without P2 columns gets ALTER via _ensure_column."""
        legacy = Path(self.temp.name) / "legacy.db"
        conn = sqlite3.connect(str(legacy))
        conn.executescript(
            """
            CREATE TABLE decisions (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                timestamp REAL NOT NULL,
                inst_id TEXT NOT NULL,
                action TEXT NOT NULL,
                confidence REAL DEFAULT 0,
                entry_price REAL,
                take_profit REAL,
                stop_loss REAL,
                reason TEXT DEFAULT '',
                calculus_data TEXT,
                raw_response TEXT,
                created_at REAL DEFAULT (strftime('%s', 'now'))
            );
            """
        )
        conn.execute(
            "INSERT INTO decisions (timestamp, inst_id, action) VALUES (?, ?, ?)",
            (time.time(), "ETH-USDT-SWAP", "WAIT"),
        )
        conn.commit()
        conn.close()

        ledger = KeelLedger(legacy)
        try:
            cols = {
                r[1]
                for r in ledger._get_conn()
                .execute("PRAGMA table_info(decisions)")
                .fetchall()
            }
            self.assertIn("policy_name", cols)
            self.assertIn("prompt_modules", cols)
            got = ledger.get_decisions(limit=10)
            self.assertEqual(len(got), 1)
            self.assertEqual(got[0].policy_name, "")
            self.assertIsNone(got[0].prompt_modules)
            ledger.record_decision(
                DecisionRecord(
                    timestamp=time.time(),
                    inst_id="BTC-USDT-SWAP",
                    action="BUY_LONG",
                    policy_name="stub",
                    prompt_modules=None,
                )
            )
            latest = ledger.get_decisions(inst_id="BTC-USDT-SWAP", limit=1)[0]
            self.assertEqual(latest.policy_name, "stub")
        finally:
            ledger.close()

    def test_cycle_persists_policy_name(self):
        summary = run_paper_cycle(
            exchange=PaperAdapter(initial_balance=10_000.0),
            ledger=self.ledger,
            policy=StubDecisionPolicy(),
            force_paper=True,
            instrument_ids=["BTC-USDT-SWAP"],
        )
        self.assertTrue(summary["ok"])
        rows = self.ledger.get_decisions(limit=5)
        self.assertGreaterEqual(len(rows), 1)
        self.assertEqual(rows[0].policy_name, "stub")
        self.assertEqual(rows[0].calculus_data.get("policy_name"), "stub")


class TestDecisionStatsEmptyAndAfterCycle(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.db = Path(self.temp.name) / "stats.db"
        set_ledger_path_override(self.db)
        os.environ["KEEL_LEDGER_DB"] = str(self.db)
        refresh_settings()
        self.ledger = KeelLedger(self.db)
        self.client = TestClient(create_app())

    def tearDown(self):
        self.ledger.close()
        set_ledger_path_override(None)
        os.environ.pop("KEEL_LEDGER_DB", None)
        refresh_settings()
        self.temp.cleanup()

    def test_stats_empty_ledger(self):
        r = self.client.get("/api/v1/stats/decisions?hours=24")
        self.assertEqual(r.status_code, 200)
        body = r.json()
        self.assertEqual(body["hours"], 24)
        self.assertEqual(body["decision_count"], 0)
        self.assertEqual(body["by_action"], {})
        self.assertEqual(body["by_policy"], {})
        self.assertEqual(body["wait_rate"], 0.0)
        self.assertEqual(body["risk_deny_events"], 0)
        self.assertEqual(body["cycle_count"], 0)
        self.assertIsNone(body["avg_cycle_duration_ms"])
        self.assertEqual(body.get("market_source", "any"), "any")

    def test_stats_hours_validation(self):
        self.assertEqual(self.client.get("/api/v1/stats/decisions?hours=0").status_code, 422)
        self.assertEqual(self.client.get("/api/v1/stats/decisions?hours=169").status_code, 422)

    def test_stats_after_paper_cycle(self):
        summary = run_paper_cycle(
            exchange=PaperAdapter(initial_balance=10_000.0),
            ledger=self.ledger,
            policy=RuleDecisionPolicy(),
            force_paper=True,
            instrument_ids=["BTC-USDT-SWAP", "ETH-USDT-SWAP"],
        )
        self.assertTrue(summary["ok"])
        # Inject a risk deny event in-window for count.
        self.ledger.record_event("risk_gate_blocked", inst_id="BTC-USDT-SWAP", data={"gate": "x"})

        r = self.client.get("/api/v1/stats/decisions")
        self.assertEqual(r.status_code, 200)
        body = r.json()
        self.assertEqual(body["hours"], 24)
        self.assertGreaterEqual(body["decision_count"], 2)
        self.assertIsInstance(body["by_action"], dict)
        self.assertGreater(sum(body["by_action"].values()), 0)
        self.assertIn("rule", body["by_policy"])
        self.assertGreaterEqual(body["by_policy"]["rule"], 2)
        self.assertGreaterEqual(body["wait_rate"], 0.0)
        self.assertLessEqual(body["wait_rate"], 1.0)
        wait_n = body["by_action"].get("WAIT", 0)
        self.assertAlmostEqual(body["wait_rate"], wait_n / body["decision_count"], places=6)
        self.assertGreaterEqual(body["risk_deny_events"], 1)
        self.assertGreaterEqual(body["cycle_count"], 1)
        self.assertIsNotNone(body["avg_cycle_duration_ms"])
        self.assertGreaterEqual(body["avg_cycle_duration_ms"], 0)

        # Decisions API exposes audit fields
        d = self.client.get("/api/v1/decisions?limit=5").json()["decisions"][0]
        self.assertEqual(d["policy_name"], "rule")
        self.assertIn("prompt_modules", d)


class TestComparePoliciesPaperScript(unittest.TestCase):
    def test_compare_script_exits_zero(self):
        script = REPO_ROOT / "scripts" / "compare_policies_paper.py"
        self.assertTrue(script.is_file())
        env = {**os.environ, "PYTHONPATH": str(REPO_ROOT)}
        for k in (
            "KEEL_OKX_API_KEY",
            "KEEL_OKX_SECRET_KEY",
            "KEEL_OKX_PASSPHRASE",
            "OKX_API_KEY",
            "OKX_SECRET_KEY",
            "OKX_PASSPHRASE",
        ):
            env[k] = ""
        proc = subprocess.run(
            [sys.executable, str(script)],
            cwd=str(REPO_ROOT),
            env=env,
            capture_output=True,
            text=True,
            timeout=60,
        )
        self.assertEqual(proc.returncode, 0, msg=proc.stderr or proc.stdout)
        self.assertIn("policy=stub", proc.stdout)
        self.assertIn("policy=rule", proc.stdout)
        self.assertIn("actions=", proc.stdout)


if __name__ == "__main__":
    unittest.main()


class TestNearestSignalsRadar(unittest.TestCase):
    """Q0: GET /api/v1/signals/nearest from recorded signal_diag decisions."""

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.db = Path(self.temp.name) / "radar.db"
        set_ledger_path_override(self.db)
        os.environ["KEEL_LEDGER_DB"] = str(self.db)
        os.environ["KEEL_INSTRUMENTS"] = "BTC-USDT-SWAP,ETH-USDT-SWAP,SOL-USDT-SWAP"
        refresh_settings()
        self.ledger = KeelLedger(self.db)
        self.client = TestClient(create_app())

    def tearDown(self):
        self.ledger.close()
        set_ledger_path_override(None)
        os.environ.pop("KEEL_LEDGER_DB", None)
        os.environ.pop("KEEL_INSTRUMENTS", None)
        refresh_settings()
        self.temp.cleanup()

    def _diag(self, nearest: str, missing: list[str], **metrics):
        base = {
            "nearest": nearest,
            "missing": missing,
            "rsi_14": 45.0,
            "trend_15m": "bullish",
            "volume_ratio": 1.2,
            "ema_9": 100.0,
            "ema_21": 99.0,
            "macd_histogram": 0.5,
            "data_valid": True,
        }
        base.update(metrics)
        return base

    def test_nearest_empty_ledger(self):
        r = self.client.get("/api/v1/signals/nearest?hours=24")
        self.assertEqual(r.status_code, 200)
        body = r.json()
        self.assertEqual(body["hours"], 24)
        self.assertEqual(body["count"], 0)
        self.assertEqual(body["signals"], [])
        s = body["summary"]
        self.assertEqual(s["waiting"], 0)
        self.assertEqual(s["long_nearest"], 0)
        self.assertEqual(s["short_nearest"], 0)
        self.assertEqual(s["fired_long"], 0)
        self.assertEqual(s["fired_short"], 0)

    def test_nearest_hours_validation(self):
        self.assertEqual(self.client.get("/api/v1/signals/nearest?hours=0").status_code, 422)
        self.assertEqual(self.client.get("/api/v1/signals/nearest?hours=169").status_code, 422)

    def test_nearest_latest_per_instrument_with_signal_diag(self):
        now = time.time()
        # Older BTC wait (should be ignored for latest)
        self.ledger.record_decision(
            DecisionRecord(
                timestamp=now - 100,
                inst_id="BTC-USDT-SWAP",
                action="WAIT",
                confidence=10.0,
                reason="old",
                calculus_data={"signal_diag": self._diag("short", ["volume_ok"])},
                policy_name="rule",
            )
        )
        # Latest BTC: near long, missing volume
        self.ledger.record_decision(
            DecisionRecord(
                timestamp=now - 10,
                inst_id="BTC-USDT-SWAP",
                action="WAIT",
                confidence=40.0,
                reason="near long",
                calculus_data={
                    "signal_diag": self._diag(
                        "long",
                        ["volume_ok", "macd_long_ok"],
                        rsi_14=28.5,
                        volume_ratio=0.4,
                    )
                },
                policy_name="rule",
            )
        )
        # ETH fired long
        self.ledger.record_decision(
            DecisionRecord(
                timestamp=now - 5,
                inst_id="ETH-USDT-SWAP",
                action="BUY_LONG",
                confidence=70.0,
                reason="fire",
                calculus_data={"signal_diag": self._diag("long", [], rsi_14=22.0)},
                policy_name="rule",
            )
        )
        # SOL near short
        self.ledger.record_decision(
            DecisionRecord(
                timestamp=now - 3,
                inst_id="SOL-USDT-SWAP",
                action="WAIT",
                confidence=35.0,
                reason="near short",
                calculus_data={
                    "signal_diag": self._diag(
                        "short",
                        ["volume_ok"],
                        trend_15m="bearish",
                        macd_histogram=-0.2,
                    )
                },
                policy_name="rule",
            )
        )
        # Outside watch list — ignored
        self.ledger.record_decision(
            DecisionRecord(
                timestamp=now - 1,
                inst_id="DOGE-USDT-SWAP",
                action="SELL_SHORT",
                confidence=60.0,
                reason="out of watch",
                calculus_data={"signal_diag": self._diag("short", [])},
                policy_name="rule",
            )
        )

        r = self.client.get("/api/v1/signals/nearest")
        self.assertEqual(r.status_code, 200)
        body = r.json()
        self.assertEqual(body["hours"], 24)
        self.assertEqual(body["count"], 3)
        by_id = {s["inst_id"]: s for s in body["signals"]}
        self.assertNotIn("DOGE-USDT-SWAP", by_id)

        btc = by_id["BTC-USDT-SWAP"]
        self.assertEqual(btc["action"], "WAIT")
        self.assertEqual(btc["nearest"], "long")
        self.assertEqual(btc["missing"], ["volume_ok", "macd_long_ok"])
        self.assertEqual(btc["rsi_14"], 28.5)
        self.assertEqual(btc["volume_ratio"], 0.4)
        self.assertEqual(btc["trend_15m"], "bullish")
        self.assertEqual(btc["ema_9"], 100.0)
        self.assertEqual(btc["ema_21"], 99.0)
        self.assertEqual(btc["macd_histogram"], 0.5)

        eth = by_id["ETH-USDT-SWAP"]
        self.assertEqual(eth["action"], "BUY_LONG")
        self.assertEqual(eth["nearest"], "long")
        self.assertEqual(eth["missing"], [])

        sol = by_id["SOL-USDT-SWAP"]
        self.assertEqual(sol["nearest"], "short")
        self.assertEqual(sol["missing"], ["volume_ok"])
        self.assertEqual(sol["trend_15m"], "bearish")

        summary = body["summary"]
        self.assertEqual(summary["waiting"], 2)
        self.assertEqual(summary["long_nearest"], 1)
        self.assertEqual(summary["short_nearest"], 1)
        self.assertEqual(summary["fired_long"], 1)
        self.assertEqual(summary["fired_short"], 0)

    def test_ledger_get_nearest_signals_direct(self):
        now = time.time()
        self.ledger.record_decision(
            DecisionRecord(
                timestamp=now,
                inst_id="BTC-USDT-SWAP",
                action="SELL_SHORT",
                calculus_data={"signal_diag": self._diag("short", [])},
            )
        )
        raw = self.ledger.get_nearest_signals(
            hours=24.0, instrument_ids=["BTC-USDT-SWAP"]
        )
        self.assertEqual(len(raw["signals"]), 1)
        self.assertEqual(raw["summary"]["fired_short"], 1)
        self.assertEqual(raw["signals"][0]["nearest"], "short")


class TestMarketSourceStampAndFilter(unittest.TestCase):
    """Q2: cycle stamps calculus_data.market_source; stats filter by it."""

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.db = Path(self.temp.name) / "ms.db"
        set_ledger_path_override(self.db)
        os.environ["KEEL_LEDGER_DB"] = str(self.db)
        refresh_settings()
        self.ledger = KeelLedger(self.db)
        self.client = TestClient(create_app())

    def tearDown(self):
        self.ledger.close()
        set_ledger_path_override(None)
        os.environ.pop("KEEL_LEDGER_DB", None)
        refresh_settings()
        self.temp.cleanup()

    def test_cycle_stamps_market_source_synthetic(self):
        summary = run_paper_cycle(
            exchange=PaperAdapter(initial_balance=10_000.0),
            ledger=self.ledger,
            policy=StubDecisionPolicy(),
            force_paper=True,
            instrument_ids=["BTC-USDT-SWAP"],
        )
        self.assertTrue(summary["ok"])
        rows = self.ledger.get_decisions(limit=5)
        self.assertGreaterEqual(len(rows), 1)
        calc = rows[0].calculus_data or {}
        self.assertEqual(calc.get("market_source"), "synthetic")

    def test_stats_market_source_filter(self):
        now = time.time()
        self.ledger.record_decision(
            DecisionRecord(
                timestamp=now,
                inst_id="BTC-USDT-SWAP",
                action="WAIT",
                calculus_data={"market_source": "synthetic", "policy_name": "rule"},
                policy_name="rule",
            )
        )
        self.ledger.record_decision(
            DecisionRecord(
                timestamp=now,
                inst_id="ETH-USDT-SWAP",
                action="BUY_LONG",
                calculus_data={"market_source": "okx_public", "policy_name": "rule"},
                policy_name="rule",
            )
        )
        self.ledger.record_decision(
            DecisionRecord(
                timestamp=now,
                inst_id="SOL-USDT-SWAP",
                action="WAIT",
                calculus_data={"policy_name": "rule"},
                policy_name="rule",
            )
        )

        any_r = self.client.get("/api/v1/stats/decisions?hours=24&market_source=any")
        self.assertEqual(any_r.status_code, 200)
        self.assertEqual(any_r.json()["decision_count"], 3)
        self.assertEqual(any_r.json()["market_source"], "any")

        syn = self.client.get("/api/v1/stats/decisions?hours=24&market_source=synthetic")
        self.assertEqual(syn.status_code, 200)
        body = syn.json()
        self.assertEqual(body["market_source"], "synthetic")
        self.assertEqual(body["decision_count"], 1)
        self.assertEqual(body["by_action"].get("WAIT"), 1)

        okx = self.client.get("/api/v1/stats/decisions?hours=24&market_source=okx_public")
        self.assertEqual(okx.status_code, 200)
        body = okx.json()
        self.assertEqual(body["market_source"], "okx_public")
        self.assertEqual(body["decision_count"], 1)
        self.assertEqual(body["by_action"].get("BUY_LONG"), 1)

        bad = self.client.get("/api/v1/stats/decisions?hours=24&market_source=nope")
        self.assertEqual(bad.status_code, 422)


class TestShadowStats(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.db = Path(self.temp.name) / "shadow.db"
        set_ledger_path_override(self.db)
        os.environ["KEEL_LEDGER_DB"] = str(self.db)
        refresh_settings()
        self.ledger = KeelLedger(self.db)
        self.client = TestClient(create_app())

    def tearDown(self):
        self.ledger.close()
        set_ledger_path_override(None)
        os.environ.pop("KEEL_LEDGER_DB", None)
        refresh_settings()
        self.temp.cleanup()

    def test_shadow_stats_empty(self):
        r = self.client.get("/api/v1/stats/shadow?hours=24")
        self.assertEqual(r.status_code, 200)
        body = r.json()
        self.assertEqual(body["hours"], 24)
        self.assertEqual(body["count"], 0)
        self.assertEqual(body["by_action"], {})
        self.assertIsNone(body["last_timestamp"])

    def test_shadow_stats_counts_by_action(self):
        t1 = time.time() - 10
        t2 = time.time() - 5
        self.ledger.record_event(
            "shadow_fill",
            inst_id="BTC-USDT-SWAP",
            data={"action": "BUY_LONG", "shadow": True},
            timestamp=t1,
        )
        self.ledger.record_event(
            "shadow_fill",
            inst_id="ETH-USDT-SWAP",
            data={"action": "SELL_SHORT", "shadow": True},
            timestamp=t2,
        )
        self.ledger.record_event(
            "shadow_fill",
            inst_id="BTC-USDT-SWAP",
            data={"action": "BUY_LONG", "shadow": True},
            timestamp=t2 + 1,
        )
        # Outside window
        self.ledger.record_event(
            "shadow_fill",
            inst_id="SOL-USDT-SWAP",
            data={"action": "BUY_LONG"},
            timestamp=time.time() - 100_000,
        )

        r = self.client.get("/api/v1/stats/shadow?hours=24")
        self.assertEqual(r.status_code, 200)
        body = r.json()
        self.assertEqual(body["count"], 3)
        self.assertEqual(body["by_action"].get("BUY_LONG"), 2)
        self.assertEqual(body["by_action"].get("SELL_SHORT"), 1)
        self.assertIsNotNone(body["last_timestamp"])
        self.assertGreaterEqual(body["last_timestamp"], t2)

        self.assertEqual(self.client.get("/api/v1/stats/shadow?hours=0").status_code, 422)
        self.assertEqual(self.client.get("/api/v1/stats/shadow?hours=169").status_code, 422)

        direct = self.ledger.get_shadow_stats(hours=24.0)
        self.assertEqual(direct["count"], 3)


class TestCompareRuleParamsScript(unittest.TestCase):
    def test_compare_rule_params_exits_zero(self):
        script = REPO_ROOT / "scripts" / "compare_rule_params.py"
        self.assertTrue(script.is_file())
        env = {**os.environ, "PYTHONPATH": str(REPO_ROOT)}
        for k in (
            "KEEL_OKX_API_KEY",
            "KEEL_OKX_SECRET_KEY",
            "KEEL_OKX_PASSPHRASE",
            "OKX_API_KEY",
            "OKX_SECRET_KEY",
            "OKX_PASSPHRASE",
        ):
            env[k] = ""
        proc = subprocess.run(
            [
                sys.executable,
                str(script),
                "--rsi-long-max-a",
                "42",
                "--rsi-short-min-a",
                "58",
                "--min-vol-a",
                "1.0",
                "--rsi-long-max-b",
                "35",
                "--rsi-short-min-b",
                "65",
                "--min-vol-b",
                "1.2",
            ],
            cwd=str(REPO_ROOT),
            env=env,
            capture_output=True,
            text=True,
            timeout=90,
        )
        self.assertEqual(proc.returncode, 0, msg=proc.stderr or proc.stdout)
        self.assertIn("set=A", proc.stdout)
        self.assertIn("set=B", proc.stdout)
        self.assertIn("actions=", proc.stdout)
        self.assertIn("near_signal_rate=", proc.stdout)


class TestQualityStats(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.db = Path(self.temp.name) / "quality.db"
        set_ledger_path_override(self.db)
        os.environ["KEEL_LEDGER_DB"] = str(self.db)
        refresh_settings()
        self.ledger = KeelLedger(self.db)
        self.client = TestClient(create_app())

    def tearDown(self):
        self.ledger.close()
        set_ledger_path_override(None)
        os.environ.pop("KEEL_LEDGER_DB", None)
        refresh_settings()
        self.temp.cleanup()

    def test_quality_empty_ledger(self):
        r = self.client.get("/api/v1/stats/quality?hours=24")
        self.assertEqual(r.status_code, 200)
        body = r.json()
        self.assertEqual(body["hours"], 24)
        self.assertEqual(body["decision_count"], 0)
        self.assertEqual(body["wait_rate"], 0.0)
        self.assertEqual(body["near_signal_rate"], 0.0)
        self.assertEqual(body["by_action"], {})
        self.assertEqual(
            body["market_source"],
            {"okx_public": 0, "synthetic": 0, "unknown": 0},
        )
        self.assertEqual(body["shadow"]["count"], 0)
        self.assertEqual(body["shadow"]["by_action"], {})
        self.assertIsNone(body["shadow"]["last_timestamp"])
        self.assertEqual(body["cycle_count"], 0)
        self.assertIsNone(body["avg_cycle_duration_ms"])

    def test_quality_scorecard_fields(self):
        now = time.time()
        self.ledger.record_decision(
            DecisionRecord(
                timestamp=now - 30,
                inst_id="BTC-USDT-SWAP",
                action="WAIT",
                confidence=10.0,
                reason="near",
                policy_name="rule",
                calculus_data={
                    "market_source": "okx_public",
                    "signal_diag": {"nearest": "long", "missing": ["vol"]},
                },
            )
        )
        self.ledger.record_decision(
            DecisionRecord(
                timestamp=now - 20,
                inst_id="ETH-USDT-SWAP",
                action="WAIT",
                confidence=10.0,
                reason="far",
                policy_name="rule",
                calculus_data={
                    "market_source": "okx_public",
                    "signal_diag": {"nearest": "none", "missing": ["rsi", "trend", "vol"]},
                },
            )
        )
        self.ledger.record_decision(
            DecisionRecord(
                timestamp=now - 10,
                inst_id="SOL-USDT-SWAP",
                action="BUY_LONG",
                confidence=80.0,
                reason="fire",
                policy_name="rule",
                calculus_data={"market_source": "synthetic"},
            )
        )
        self.ledger.record_decision(
            DecisionRecord(
                timestamp=now - 5,
                inst_id="XRP-USDT-SWAP",
                action="WAIT",
                confidence=5.0,
                reason="no ms",
                policy_name="rule",
                calculus_data={"signal_diag": {"nearest": "short"}},
            )
        )
        self.ledger.record_event(
            "shadow_fill",
            inst_id="BTC-USDT-SWAP",
            data={"action": "BUY_LONG", "shadow": True},
            timestamp=now - 2,
        )
        self.ledger.record_event(
            self.ledger.CYCLE_SUMMARY_EVENT,
            data={"duration_ms": 100.0, "ok": True},
            timestamp=now - 1,
        )
        self.ledger.record_event(
            self.ledger.CYCLE_SUMMARY_EVENT,
            data={"duration_ms": 200.0, "ok": True},
            timestamp=now,
        )

        r = self.client.get("/api/v1/stats/quality?hours=24")
        self.assertEqual(r.status_code, 200)
        body = r.json()
        self.assertEqual(body["decision_count"], 4)
        self.assertEqual(body["by_action"].get("WAIT"), 3)
        self.assertEqual(body["by_action"].get("BUY_LONG"), 1)
        self.assertAlmostEqual(body["wait_rate"], 0.75, places=5)
        # 2 of 3 WAIT have nearest in {long, short}
        self.assertAlmostEqual(body["near_signal_rate"], 2.0 / 3.0, places=5)
        self.assertEqual(body["market_source"]["okx_public"], 2)
        self.assertEqual(body["market_source"]["synthetic"], 1)
        self.assertEqual(body["market_source"]["unknown"], 1)
        self.assertEqual(body["shadow"]["count"], 1)
        self.assertEqual(body["shadow"]["by_action"].get("BUY_LONG"), 1)
        self.assertIsNotNone(body["shadow"]["last_timestamp"])
        self.assertEqual(body["cycle_count"], 2)
        self.assertAlmostEqual(body["avg_cycle_duration_ms"], 150.0, places=5)

        self.assertEqual(self.client.get("/api/v1/stats/quality?hours=0").status_code, 422)
        self.assertEqual(self.client.get("/api/v1/stats/quality?hours=169").status_code, 422)

        direct = self.ledger.get_quality_stats(hours=24.0)
        self.assertEqual(direct["decision_count"], 4)
        self.assertAlmostEqual(direct["near_signal_rate"], 2.0 / 3.0, places=5)
