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
