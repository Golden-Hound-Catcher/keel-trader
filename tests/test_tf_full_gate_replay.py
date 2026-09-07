"""E2C: offline TF full-gate fire-rate replay helpers + script smoke."""
from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path

from keel.domain.records import DecisionRecord, FactorSnapshot
from keel.factors.market_data import MarketSnapshot
from keel.ledger import KeelLedger
from keel.ledger.decision_export import snapshot_from_export_row
from keel.ledger.tf_fire_replay import (
    diagnose_crafted,
    forced_rule_variant,
    load_replay_rows,
    normalize_variant,
    replay_under_variant,
    rows_from_factor_snapshots,
    summarize_replay,
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


def _tf_long_snap(**overrides) -> MarketSnapshot:
    """Crafted TF long that should full-gate under E2B soft_tf (mid RSI, soft vol)."""
    base = dict(
        inst_id="BTC-USDT-SWAP",
        name="BTC",
        timestamp=time.time(),
        price=65000.0,
        atr_14=400.0,
        rsi_14=52.0,
        ema_9=65100.0,
        ema_21=64800.0,
        macd_histogram=5.0,
        volume_ratio=0.40,
        volume_percentile=40.0,
        trend_15m="bullish",
        trend_1h="bullish",
        trend_4h="bullish",
        data_valid=True,
        data_quality_reason="test",
    )
    base.update(overrides)
    return MarketSnapshot(**base)  # type: ignore[arg-type]


class TestTfFireReplayHelpers(unittest.TestCase):
    def tearDown(self) -> None:
        os.environ.pop("KEEL_RULE_VARIANT", None)

    def test_normalize_variant(self):
        self.assertEqual(normalize_variant("tf"), "trend_follow")
        self.assertEqual(normalize_variant("mean_revert"), "mean_revert")
        self.assertEqual(normalize_variant(None), "trend_follow")

    def test_forced_variant_restores(self):
        os.environ["KEEL_RULE_VARIANT"] = "mean_revert"
        with forced_rule_variant("trend_follow") as v:
            self.assertEqual(v, "trend_follow")
            self.assertEqual(os.environ["KEEL_RULE_VARIANT"], "trend_follow")
        self.assertEqual(os.environ["KEEL_RULE_VARIANT"], "mean_revert")

    def test_crafted_tf_full_gate_soft_tf(self):
        out = diagnose_crafted(_tf_long_snap(), variant="trend_follow")
        self.assertEqual(out["action"], "BUY_LONG")
        self.assertTrue(out["full_gate"])
        self.assertEqual(out["signal_diag"]["volume_path"], "soft_tf")
        self.assertEqual(out["signal_diag"]["rule_variant"], "trend_follow")

    def test_crafted_mr_does_not_soft_without_rsi_extreme(self):
        out = diagnose_crafted(_tf_long_snap(), variant="mean_revert")
        self.assertNotEqual(out["action"], "BUY_LONG")
        self.assertFalse(out["full_gate"])

    def test_summarize_replay_counts_near_1(self):
        results = [
            {
                "inst_id": "BTC-USDT-SWAP",
                "action": "BUY_LONG",
                "skipped": False,
                "signal_diag": {
                    "missing": [],
                    "nearest": "long",
                    "trend_1h": "bullish",
                    "volume_path": "soft_tf",
                },
            },
            {
                "inst_id": "ETH-USDT-SWAP",
                "action": "WAIT",
                "skipped": False,
                "signal_diag": {
                    "missing": ["volume_ok"],
                    "nearest": "short",
                    "trend_1h": "bearish",
                },
            },
            {"inst_id": "SOL-USDT-SWAP", "action": None, "skipped": True},
        ]
        s = summarize_replay(results, variant="trend_follow")
        self.assertEqual(s["n_snapshots"], 2)
        self.assertEqual(s["skipped_incomplete"], 1)
        self.assertEqual(s["full_gate_count"], 1)
        self.assertEqual(s["near_1_missing_count"], 1)
        self.assertEqual(s["top_missing_gates"].get("volume_ok"), 1)
        self.assertTrue(s["markout"]["skipped"])

    def test_snapshot_reconstructs_trend_1h_from_factors(self):
        row = {
            "inst_id": "BTC-USDT-SWAP",
            "timestamp": 1.0,
            "factors": {
                "rsi_14": 50.0,
                "ema_9": 1.0,
                "ema_21": 0.9,
                "macd_histogram": 1.0,
                "trend_15m": "bullish",
                "trend_1h": "bullish",
                "trend_4h": "neutral",
                "volume_ratio": 0.5,
                "volume_percentile": 60.0,
                "price": 100.0,
                "atr_14": 1.0,
                "data_valid": True,
            },
        }
        snap = snapshot_from_export_row(row)
        assert snap is not None
        self.assertEqual(snap.trend_1h, "bullish")
        self.assertEqual(snap.trend_4h, "neutral")
        self.assertAlmostEqual(snap.volume_percentile or 0.0, 60.0)

    def test_replay_under_variant_on_factor_rows(self):
        snaps = [
            FactorSnapshot(
                timestamp=time.time(),
                inst_id="BTC-USDT-SWAP",
                price=65000.0,
                rsi_14=52.0,
                ema_9=65100.0,
                ema_21=64800.0,
                atr_14=400.0,
                macd_histogram=5.0,
                trend_15m="bullish",
                volume_ratio=0.40,
                payload={
                    "trend_1h": "bullish",
                    "trend_4h": "bullish",
                    "data_valid": True,
                },
            )
        ]
        rows = rows_from_factor_snapshots(snaps)
        _results, summary = replay_under_variant(rows, "trend_follow")
        self.assertEqual(summary["full_gate_count"], 1)
        self.assertGreater(summary["full_gate_rate"], 0.0)


class TestTfFireReplayLedgerSmoke(unittest.TestCase):
    def test_load_and_replay_tiny_ledger(self):
        with tempfile.TemporaryDirectory() as tmp:
            db = Path(tmp) / "t.db"
            ledger = KeelLedger(db)
            try:
                now = time.time()
                ledger.record_factor_snapshot(
                    FactorSnapshot(
                        timestamp=now,
                        inst_id="BTC-USDT-SWAP",
                        price=65000.0,
                        rsi_14=52.0,
                        ema_9=65100.0,
                        ema_21=64800.0,
                        atr_14=400.0,
                        macd_histogram=5.0,
                        trend_15m="bullish",
                        volume_ratio=0.40,
                        payload={
                            "trend_1h": "bullish",
                            "trend_4h": "bullish",
                            "data_valid": True,
                        },
                    )
                )
                ledger.record_decision(
                    DecisionRecord(
                        timestamp=now,
                        inst_id="BTC-USDT-SWAP",
                        action="WAIT",
                        entry_price=65000.0,
                        policy_name="rule",
                        calculus_data={
                            "market_source": "okx_public",
                            "signal_diag": {
                                "rsi_14": 52.0,
                                "ema_9": 65100.0,
                                "ema_21": 64800.0,
                                "macd_histogram": 5.0,
                                "trend_15m": "bullish",
                                "trend_1h": "bullish",
                                "trend_4h": "bullish",
                                "volume_ratio": 0.40,
                                "volume_percentile": 40.0,
                                "atr_bps": 60.0,
                                "data_valid": True,
                                "missing": ["volume_ok"],
                                "nearest": "long",
                            },
                        },
                    )
                )
                rows = load_replay_rows(ledger, hours=1.0, prefer="decisions")
                self.assertGreaterEqual(len(rows), 1)
                _r, summary = replay_under_variant(rows, "trend_follow")
                self.assertEqual(summary["full_gate_count"], 1)
            finally:
                ledger.close()


class TestTfFireReplayScript(unittest.TestCase):
    def test_script_missing_db_exits_zero(self):
        env = _clear_okx_env(os.environ.copy())
        missing = Path(tempfile.mkdtemp()) / "nope.db"
        proc = subprocess.run(
            [
                sys.executable,
                str(REPO_ROOT / "scripts" / "tf_full_gate_replay.py"),
                "--db",
                str(missing),
                "--hours",
                "1",
            ],
            cwd=str(REPO_ROOT),
            env=env,
            capture_output=True,
            text=True,
            check=False,
        )
        self.assertEqual(proc.returncode, 0)
        self.assertIn("DB missing", proc.stderr)

    def test_script_on_temp_db(self):
        env = _clear_okx_env(os.environ.copy())
        with tempfile.TemporaryDirectory() as tmp:
            db = Path(tmp) / "t.db"
            ledger = KeelLedger(db)
            try:
                now = time.time()
                ledger.record_factor_snapshot(
                    FactorSnapshot(
                        timestamp=now,
                        inst_id="BTC-USDT-SWAP",
                        price=65000.0,
                        rsi_14=52.0,
                        ema_9=65100.0,
                        ema_21=64800.0,
                        atr_14=400.0,
                        macd_histogram=5.0,
                        trend_15m="bullish",
                        volume_ratio=0.40,
                        payload={
                            "trend_1h": "bullish",
                            "trend_4h": "bullish",
                            "data_valid": True,
                        },
                    )
                )
                ledger.record_decision(
                    DecisionRecord(
                        timestamp=now,
                        inst_id="BTC-USDT-SWAP",
                        action="WAIT",
                        entry_price=65000.0,
                        policy_name="rule",
                        calculus_data={
                            "market_source": "okx_public",
                            "signal_diag": {
                                "rsi_14": 52.0,
                                "ema_9": 65100.0,
                                "ema_21": 64800.0,
                                "macd_histogram": 5.0,
                                "trend_15m": "bullish",
                                "trend_1h": "bullish",
                                "trend_4h": "bullish",
                                "volume_ratio": 0.40,
                                "data_valid": True,
                                "missing": ["volume_ok"],
                                "nearest": "long",
                            },
                        },
                    )
                )
            finally:
                ledger.close()

            out_json = Path(tmp) / "out.json"
            proc = subprocess.run(
                [
                    sys.executable,
                    str(REPO_ROOT / "scripts" / "tf_full_gate_replay.py"),
                    "--db",
                    str(db),
                    "--hours",
                    "1",
                    "--variant",
                    "trend_follow",
                    "--compare-mr",
                    "--json-out",
                    str(out_json),
                ],
                cwd=str(REPO_ROOT),
                env=env,
                capture_output=True,
                text=True,
                check=False,
            )
            self.assertEqual(proc.returncode, 0, proc.stderr + proc.stdout)
            self.assertIn("full_gate=", proc.stdout)
            data = json.loads(out_json.read_text(encoding="utf-8"))
            self.assertEqual(data["primary"]["full_gate_count"], 1)
            self.assertIn("compare_mean_revert", data)


if __name__ == "__main__":
    unittest.main()
