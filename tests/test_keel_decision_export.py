"""Q2.1: export decisions + compare_rule_params --from-ledger / --db."""
from __future__ import annotations

import json
import shutil
import os
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path

from keel.domain.records import DecisionRecord, FactorSnapshot
from keel.ledger import KeelLedger
from keel.ledger.decision_export import (
    action_histogram,
    is_replayable_factors,
    load_export_path,
    near_signal_rate,
    replay_rule_on_rows,
    snapshot_from_export_row,
)

REPO_ROOT = Path(__file__).resolve().parents[1]


def _clear_okx_env(env: dict[str, str]) -> dict[str, str]:
    out = dict(env)
    for k in (
        "KEEL_OKX_API_KEY",
        "KEEL_OKX_SECRET_KEY",
        "KEEL_OKX_PASSPHRASE",
        "OKX_API_KEY",
        "OKX_SECRET_KEY",
        "OKX_PASSPHRASE",
        "OKX_DEMO_API_KEY",
        "OKX_DEMO_SECRET_KEY",
        "OKX_DEMO_PASSPHRASE",
    ):
        out[k] = ""
    out["PYTHONPATH"] = str(REPO_ROOT)
    out["KEEL_SKIP_DOTENV"] = "1"
    return out


class TestDecisionExportHelpers(unittest.TestCase):
    def test_snapshot_from_signal_diag_without_price_atr(self):
        row = {
            "inst_id": "BTC-USDT-SWAP",
            "instrument": "BTC-USDT-SWAP",
            "timestamp": time.time(),
            "action": "WAIT",
            "entry_price": None,
            "calculus_data": {
                "market_source": "okx_public",
                "rsi_14": 40.0,
                "trend_15m": "bullish",
                "signal_diag": {
                    "data_valid": True,
                    "rsi_14": 40.0,
                    "volume_ratio": 1.2,
                    "ema_9": 101.0,
                    "ema_21": 100.0,
                    "macd_histogram": 0.5,
                    "trend_15m": "bullish",
                    "nearest": "long",
                    "missing": ["volume_ok"],
                },
            },
            "factors": None,
        }
        # factors merged from signal_diag via snapshot_from_export_row
        snap = snapshot_from_export_row(row)
        self.assertIsNotNone(snap)
        assert snap is not None
        self.assertEqual(snap.trend_15m, "bullish")
        self.assertGreater(snap.price, 0)
        self.assertGreater(snap.atr_14, 0)

    def test_incomplete_row_skipped(self):
        row = {
            "inst_id": "ETH-USDT-SWAP",
            "timestamp": time.time(),
            "action": "WAIT",
            "calculus_data": {"market_source": "okx_public", "rsi_14": 50},
            "factors": None,
        }
        snap = snapshot_from_export_row(row)
        self.assertIsNone(snap)
        results, replayed, skipped = replay_rule_on_rows([row])
        self.assertEqual(replayed, 0)
        self.assertEqual(skipped, 1)
        self.assertTrue(results[0]["skipped"])

    def test_replay_ab_changes_with_thresholds(self):
        # Near-long: RSI 40, bullish, macd+, ema stack, vol 1.1
        row = {
            "inst_id": "BTC-USDT-SWAP",
            "timestamp": time.time(),
            "action": "WAIT",
            "factors": {
                "price": 50000.0,
                "atr_14": 100.0,
                "rsi_14": 40.0,
                "ema_9": 50100.0,
                "ema_21": 50000.0,
                "macd_histogram": 1.0,
                "trend_15m": "bullish",
                "volume_ratio": 1.1,
                "data_valid": True,
            },
            "calculus_data": {"market_source": "okx_public"},
            "replayable": True,
        }
        keys = (
            "KEEL_RULE_RSI_LONG_MAX",
            "KEEL_RULE_RSI_SHORT_MIN",
            "KEEL_RULE_MIN_VOLUME_RATIO",
        )
        saved = {k: os.environ.get(k) for k in keys}
        try:
            os.environ["KEEL_RULE_RSI_LONG_MAX"] = "42"
            os.environ["KEEL_RULE_RSI_SHORT_MIN"] = "58"
            os.environ["KEEL_RULE_MIN_VOLUME_RATIO"] = "1.0"
            res_a, n_a, skip_a = replay_rule_on_rows([row])
            self.assertEqual(skip_a, 0)
            self.assertEqual(n_a, 1)
            self.assertEqual(res_a[0]["action"], "BUY_LONG")

            os.environ["KEEL_RULE_RSI_LONG_MAX"] = "35"
            os.environ["KEEL_RULE_MIN_VOLUME_RATIO"] = "1.5"
            res_b, n_b, skip_b = replay_rule_on_rows([row])
            self.assertEqual(skip_b, 0)
            self.assertEqual(n_b, 1)
            self.assertEqual(res_b[0]["action"], "WAIT")
            self.assertEqual(action_histogram(res_b).get("WAIT"), 1)
            self.assertGreaterEqual(near_signal_rate(res_b), 0.0)
        finally:
            for k, v in saved.items():
                if v is None:
                    os.environ.pop(k, None)
                else:
                    os.environ[k] = v


class TestLedgerExportDecisions(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.mkdtemp()
        self.db_path = Path(self.temp_dir) / "ledger.db"
        self.ledger = KeelLedger(self.db_path)
        self.now = time.time()

    def tearDown(self):
        self.ledger.close()
        shutil.rmtree(self.temp_dir, ignore_errors=True)

    def _seed(self):
        ts = self.now
        self.ledger.record_factor_snapshot(
            FactorSnapshot(
                timestamp=ts,
                inst_id="BTC-USDT-SWAP",
                price=50000.0,
                rsi_14=40.0,
                ema_9=50100.0,
                ema_21=50000.0,
                atr_14=100.0,
                macd_histogram=1.0,
                trend_15m="bullish",
                volume_ratio=1.2,
                payload={"data_valid": True, "data_quality_reason": "okx_public"},
            )
        )
        self.ledger.record_decision(
            DecisionRecord(
                timestamp=ts,
                inst_id="BTC-USDT-SWAP",
                action="WAIT",
                confidence=40.0,
                policy_name="rule",
                calculus_data={
                    "rsi_14": 40.0,
                    "trend_15m": "bullish",
                    "market_source": "okx_public",
                    "policy_name": "rule",
                    "signal_diag": {
                        "data_valid": True,
                        "rsi_14": 40.0,
                        "volume_ratio": 1.2,
                        "ema_9": 50100.0,
                        "ema_21": 50000.0,
                        "macd_histogram": 1.0,
                        "trend_15m": "bullish",
                        "nearest": "long",
                        "missing": [],
                    },
                },
            )
        )
        # Incomplete older synthetic without factors joinable at different ts
        self.ledger.record_decision(
            DecisionRecord(
                timestamp=ts - 10,
                inst_id="ETH-USDT-SWAP",
                action="WAIT",
                policy_name="rule",
                calculus_data={"market_source": "synthetic", "rsi_14": 55},
            )
        )

    def test_export_decisions_filters_and_joins_factors(self):
        self._seed()
        rows = self.ledger.export_decisions(
            hours=1, market_source="okx_public", limit=10
        )
        self.assertEqual(len(rows), 1)
        row = rows[0]
        self.assertEqual(row["instrument"], "BTC-USDT-SWAP")
        self.assertEqual(row["market_source"], "okx_public")
        self.assertEqual(row["policy_name"], "rule")
        self.assertIsInstance(row["signal_diag"], dict)
        self.assertTrue(row["replayable"])
        self.assertIsNotNone(row["factors"])
        self.assertAlmostEqual(float(row["factors"]["price"]), 50000.0)

        all_rows = self.ledger.export_decisions(hours=1, market_source="any")
        self.assertEqual(len(all_rows), 2)

    def test_export_script_jsonl(self):
        self._seed()
        script = REPO_ROOT / "scripts" / "export_decisions.py"
        out = Path(self.temp_dir) / "out.jsonl"
        env = _clear_okx_env(os.environ)
        proc = subprocess.run(
            [
                sys.executable,
                str(script),
                "--db",
                str(self.db_path),
                "--hours",
                "1",
                "--market-source",
                "okx_public",
                "--format",
                "jsonl",
                "--out",
                str(out),
            ],
            cwd=str(REPO_ROOT),
            env=env,
            capture_output=True,
            text=True,
            timeout=60,
        )
        self.assertEqual(proc.returncode, 0, msg=proc.stderr or proc.stdout)
        self.assertTrue(out.is_file())
        loaded = load_export_path(out)
        self.assertEqual(len(loaded), 1)
        self.assertEqual(loaded[0]["market_source"], "okx_public")


class TestCompareFromLedgerScript(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.mkdtemp()
        self.db_path = Path(self.temp_dir) / "ledger.db"
        self.ledger = KeelLedger(self.db_path)
        ts = time.time()
        self.ledger.record_factor_snapshot(
            FactorSnapshot(
                timestamp=ts,
                inst_id="BTC-USDT-SWAP",
                price=50000.0,
                rsi_14=40.0,
                ema_9=50100.0,
                ema_21=50000.0,
                atr_14=100.0,
                macd_histogram=1.0,
                trend_15m="bullish",
                volume_ratio=1.1,
                payload={"data_valid": True, "data_quality_reason": "okx_public"},
            )
        )
        self.ledger.record_decision(
            DecisionRecord(
                timestamp=ts,
                inst_id="BTC-USDT-SWAP",
                action="BUY_LONG",
                policy_name="rule",
                calculus_data={
                    "market_source": "okx_public",
                    "rsi_14": 40.0,
                    "trend_15m": "bullish",
                    "signal_diag": {
                        "data_valid": True,
                        "rsi_14": 40.0,
                        "volume_ratio": 1.1,
                        "ema_9": 50100.0,
                        "ema_21": 50000.0,
                        "macd_histogram": 1.0,
                        "trend_15m": "bullish",
                        "nearest": "long",
                        "missing": [],
                    },
                },
            )
        )
        # incomplete — should be skipped
        self.ledger.record_decision(
            DecisionRecord(
                timestamp=ts - 1,
                inst_id="SOL-USDT-SWAP",
                action="WAIT",
                policy_name="rule",
                calculus_data={"market_source": "okx_public"},
            )
        )
        self.ledger.close()

    def tearDown(self):
        shutil.rmtree(self.temp_dir, ignore_errors=True)

    def test_compare_from_db(self):
        script = REPO_ROOT / "scripts" / "compare_rule_params.py"
        env = _clear_okx_env(os.environ)
        proc = subprocess.run(
            [
                sys.executable,
                str(script),
                "--db",
                str(self.db_path),
                "--hours",
                "1",
                "--market-source",
                "okx_public",
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
                "1.5",
            ],
            cwd=str(REPO_ROOT),
            env=env,
            capture_output=True,
            text=True,
            timeout=60,
        )
        self.assertEqual(proc.returncode, 0, msg=proc.stderr or proc.stdout)
        self.assertIn("ledger replay", proc.stdout)
        self.assertIn("set=A", proc.stdout)
        self.assertIn("set=B", proc.stdout)
        self.assertIn("skipped_incomplete=", proc.stdout)
        self.assertIn("BUY_LONG", proc.stdout)  # set A should fire
        self.assertIn("done", proc.stdout)

    def test_compare_from_ledger_file(self):
        # export then compare
        ledger = KeelLedger(self.db_path)
        rows = ledger.export_decisions(hours=1, market_source="okx_public")
        ledger.close()
        export_path = Path(self.temp_dir) / "dec.jsonl"
        export_path.write_text(
            "".join(json.dumps(r) + "\n" for r in rows), encoding="utf-8"
        )
        script = REPO_ROOT / "scripts" / "compare_rule_params.py"
        env = _clear_okx_env(os.environ)
        proc = subprocess.run(
            [
                sys.executable,
                str(script),
                "--from-ledger",
                str(export_path),
                "--rsi-long-max-a",
                "42",
                "--min-vol-a",
                "1.0",
                "--rsi-long-max-b",
                "35",
                "--min-vol-b",
                "1.5",
            ],
            cwd=str(REPO_ROOT),
            env=env,
            capture_output=True,
            text=True,
            timeout=60,
        )
        self.assertEqual(proc.returncode, 0, msg=proc.stderr or proc.stdout)
        self.assertIn("set=A", proc.stdout)
        self.assertIn("set=B", proc.stdout)


class TestIsReplayable(unittest.TestCase):
    def test_gate_keys_required(self):
        self.assertFalse(is_replayable_factors(None))
        self.assertFalse(is_replayable_factors({"rsi_14": 1}))
        self.assertTrue(
            is_replayable_factors(
                {
                    "rsi_14": 40,
                    "ema_9": 1,
                    "ema_21": 1,
                    "macd_histogram": 0,
                    "trend_15m": "neutral",
                    "volume_ratio": 1.0,
                }
            )
        )


if __name__ == "__main__":
    unittest.main()
