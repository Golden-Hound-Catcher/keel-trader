"""Tests for Keel paper/demo vertical cycle (Stage 2)."""
from __future__ import annotations

import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from keel.exchange.paper import PaperAdapter
from keel.ledger import KeelLedger
from keel.worker.cycle import (
    CYCLE_ERRORS_CAP,
    RISK_DENY_REASONS_CAP,
    build_cycle_summary,
    build_synthetic_candles,
    enrich_snapshot,
    rule_based_decision,
    run_paper_cycle,
)
from keel.worker.scheduler import KeelScheduler, JobSpec
from keel.factors.market_data import MarketSnapshot


class TestSyntheticFactors(unittest.TestCase):
    def test_build_and_enrich_snapshot(self):
        candles = build_synthetic_candles(65000.0, count=64)
        self.assertEqual(len(candles), 64)
        snap = MarketSnapshot(
            inst_id="BTC-USDT-SWAP",
            name="BTC",
            timestamp=candles[-1].timestamp,
            candles_15m=candles,
        )
        enrich_snapshot(snap)
        self.assertTrue(snap.data_valid)
        self.assertGreater(snap.price, 0)
        self.assertGreater(snap.atr_14, 0)
        decision = rule_based_decision(snap)
        self.assertIn(decision.action, ("BUY_LONG", "SELL_SHORT", "WAIT"))


class TestPaperCycle(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.db = Path(self.temp.name) / "cycle.db"
        self.ledger = KeelLedger(self.db)
        self.exchange = PaperAdapter(initial_balance=10_000.0)

    def tearDown(self):
        self.ledger.close()
        self.temp.cleanup()

    def test_wait_cycle_records_decisions(self):
        summary = run_paper_cycle(
            exchange=self.exchange,
            ledger=self.ledger,
            instrument_ids=["BTC-USDT-SWAP"],
            force_action="WAIT",
        )
        self.assertTrue(summary["ok"])
        self.assertEqual(summary["branding"], "Keel Trader")
        self.assertEqual(summary["results"][0]["action"], "WAIT")
        latest = self.ledger.get_latest_decision("BTC-USDT-SWAP", max_age_seconds=60)
        self.assertIsNotNone(latest)
        self.assertEqual(latest.action, "WAIT")

    def test_forced_long_executes_and_ledgers_trade(self):
        summary = run_paper_cycle(
            exchange=self.exchange,
            ledger=self.ledger,
            instrument_ids=["BTC-USDT-SWAP"],
            force_action="BUY_LONG",
        )
        self.assertTrue(summary["ok"])
        result = summary["results"][0]
        self.assertEqual(result["action"], "BUY_LONG")
        self.assertTrue(result["success"], msg=result)
        self.assertIsNotNone(result["order_id"])
        trades = self.ledger.get_trades(inst_id="BTC-USDT-SWAP")
        self.assertGreaterEqual(len(trades), 1)
        self.assertEqual(len(self.exchange.get_positions()), 1)

    def test_multi_instrument_paper_cycle(self):
        summary = run_paper_cycle(
            exchange=self.exchange,
            ledger=self.ledger,
            instrument_ids=["BTC-USDT-SWAP", "ETH-USDT-SWAP", "SOL-USDT-SWAP"],
        )
        self.assertEqual(summary["instruments"], 3)
        self.assertEqual(len(summary["results"]), 3)

    def test_forced_short_executes_and_ledgers_trade(self):
        summary = run_paper_cycle(
            exchange=self.exchange,
            ledger=self.ledger,
            instrument_ids=["BTC-USDT-SWAP"],
            force_action="SELL_SHORT",
        )
        self.assertTrue(summary["ok"])
        result = summary["results"][0]
        self.assertEqual(result["action"], "SELL_SHORT")
        self.assertTrue(result["success"], msg=result)
        self.assertTrue(result.get("filled"))
        trades = self.ledger.get_trades(inst_id="BTC-USDT-SWAP")
        self.assertGreaterEqual(len(trades), 1)
        self.assertEqual(trades[0].direction, "short")

    def test_cycle_persists_factor_snapshots(self):
        run_paper_cycle(
            exchange=self.exchange,
            ledger=self.ledger,
            instrument_ids=["BTC-USDT-SWAP", "ETH-USDT-SWAP"],
            force_action="WAIT",
        )
        btc = self.ledger.get_latest_factor_snapshot("BTC-USDT-SWAP", max_age_seconds=60)
        self.assertIsNotNone(btc)
        self.assertGreater(btc.price, 0)
        snaps = self.ledger.get_factor_snapshots(limit=10)
        self.assertGreaterEqual(len(snaps), 2)

    def test_cycle_writes_structured_summary(self):
        summary = run_paper_cycle(
            exchange=self.exchange,
            ledger=self.ledger,
            instrument_ids=["BTC-USDT-SWAP", "ETH-USDT-SWAP"],
            force_action="WAIT",
        )
        self.assertIn("cycle_summary", summary)
        cs = summary["cycle_summary"]
        self.assertEqual(cs["mode"], "paper")
        self.assertEqual(cs["instruments"], 2)
        self.assertEqual(cs["decision_counts"].get("WAIT"), 2)
        self.assertEqual(cs["risk_denies"], 0)
        self.assertEqual(cs["risk_deny_reasons"], [])
        self.assertEqual(cs["error_count"], 0)
        self.assertEqual(cs["errors"], [])
        self.assertIn("duration_ms", cs)
        self.assertIsInstance(cs["duration_ms"], int)
        self.assertGreaterEqual(cs["duration_ms"], 0)

        stored = self.ledger.get_last_cycle_summary()
        self.assertIsNotNone(stored)
        self.assertEqual(stored["instruments"], 2)
        self.assertEqual(stored["decision_counts"].get("WAIT"), 2)
        self.assertEqual(stored["duration_ms"], cs["duration_ms"])
        events = self.ledger.get_events(event_type="worker_cycle_summary", limit=5)
        self.assertGreaterEqual(len(events), 1)


class TestBuildCycleSummaryRiskDenies(unittest.TestCase):
    def test_collects_gate_and_reason(self):
        cs = build_cycle_summary(
            timestamp=1.0,
            mode="paper",
            adapter="paper",
            policy="rule",
            instruments=2,
            results=[
                {
                    "inst_id": "BTC-USDT-SWAP",
                    "action": "BUY_LONG",
                    "success": False,
                    "risk_gate_failed": "kill_switch",
                    "error": "Kill switch active",
                },
                {
                    "inst_id": "ETH-USDT-SWAP",
                    "action": "WAIT",
                    "success": True,
                },
            ],
        )
        self.assertEqual(cs["risk_denies"], 1)
        self.assertEqual(len(cs["risk_deny_reasons"]), 1)
        self.assertEqual(cs["risk_deny_reasons"][0]["gate"], "kill_switch")
        self.assertEqual(cs["risk_deny_reasons"][0]["reason"], "Kill switch active")
        self.assertEqual(cs["error_count"], 0)
        self.assertEqual(cs["errors"], [])

    def test_caps_reason_list(self):
        results = [
            {
                "inst_id": f"X{i}",
                "action": "BUY_LONG",
                "success": False,
                "risk_gate_failed": "max_positions",
                "error": f"deny-{i}",
            }
            for i in range(RISK_DENY_REASONS_CAP + 5)
        ]
        cs = build_cycle_summary(
            timestamp=1.0,
            mode="paper",
            adapter="paper",
            policy="rule",
            instruments=len(results),
            results=results,
        )
        self.assertEqual(cs["risk_denies"], RISK_DENY_REASONS_CAP + 5)
        self.assertEqual(len(cs["risk_deny_reasons"]), RISK_DENY_REASONS_CAP)
        self.assertEqual(cs["risk_deny_reasons"][0]["reason"], "deny-0")
        self.assertEqual(
            cs["risk_deny_reasons"][-1]["reason"],
            f"deny-{RISK_DENY_REASONS_CAP - 1}",
        )


class TestBuildCycleSummaryErrors(unittest.TestCase):
    def test_collects_error_count_and_detail(self):
        cs = build_cycle_summary(
            timestamp=1.0,
            mode="paper",
            adapter="paper",
            policy="rule",
            instruments=2,
            results=[
                {
                    "inst_id": "BTC-USDT-SWAP",
                    "action": "BUY_LONG",
                    "success": False,
                    "error": "timeout",
                },
                {
                    "inst_id": "ETH-USDT-SWAP",
                    "action": "WAIT",
                    "success": True,
                },
            ],
        )
        self.assertEqual(cs["error_count"], 1)
        self.assertEqual(len(cs["errors"]), 1)
        self.assertEqual(cs["errors"][0]["inst_id"], "BTC-USDT-SWAP")
        self.assertEqual(cs["errors"][0]["error"], "timeout")
        self.assertEqual(cs["risk_denies"], 0)

    def test_caps_errors_list_keeps_full_count(self):
        results = [
            {
                "inst_id": f"X{i}",
                "action": "BUY_LONG",
                "success": False,
                "error": f"err-{i}",
            }
            for i in range(CYCLE_ERRORS_CAP + 5)
        ]
        cs = build_cycle_summary(
            timestamp=1.0,
            mode="paper",
            adapter="paper",
            policy="rule",
            instruments=len(results),
            results=results,
        )
        self.assertEqual(cs["error_count"], CYCLE_ERRORS_CAP + 5)
        self.assertEqual(len(cs["errors"]), CYCLE_ERRORS_CAP)
        self.assertEqual(cs["errors"][0]["error"], "err-0")
        self.assertEqual(
            cs["errors"][-1]["error"],
            f"err-{CYCLE_ERRORS_CAP - 1}",
        )


class TestKeelSchedulerTraderJob(unittest.TestCase):

    def test_trader_job_invokes_keel_cycle_module(self):
        scheduler = KeelScheduler(jobs=[JobSpec("trader", interval_seconds=60, timeout_seconds=30)])
        with patch("keel.worker.scheduler.subprocess.run") as run_mock:
            run_mock.return_value = type("R", (), {"returncode": 0, "stdout": "ok", "stderr": ""})()
            scheduler._run_job(scheduler._jobs["trader"])
            args = run_mock.call_args[0][0]
            self.assertEqual(args[1:3], ["-m", "keel.worker.cycle"])
        scheduler._executor.shutdown(wait=False, cancel_futures=True)


class TestLegacySchedulerDisabled(unittest.TestCase):
    def test_backend_scheduler_refuses(self):
        import subprocess, sys
        result = subprocess.run(
            [sys.executable, "-m", "r20_backend.scheduler"],
            cwd=Path(__file__).resolve().parents[1],
            capture_output=True,
            text=True,
            timeout=15,
        )
        self.assertEqual(result.returncode, 2)
        self.assertIn("DISABLED", result.stderr)

    def test_keel_worker_cycle_entrypoint(self):
        import subprocess, sys
        env = os.environ.copy()
        with tempfile.TemporaryDirectory() as tmp:
            db = Path(tmp) / "cycle.db"
            result = subprocess.run(
                [sys.executable, "-m", "keel.worker.cycle", "--db", str(db)],
                cwd=Path(__file__).resolve().parents[1],
                capture_output=True,
                text=True,
                timeout=30,
                env=env,
            )
            self.assertEqual(result.returncode, 0, msg=result.stderr + result.stdout)
            self.assertIn("Keel Trader", result.stdout)


if __name__ == "__main__":
    unittest.main()
