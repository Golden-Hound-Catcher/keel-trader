"""R9 offline near-entry markout — counterfactual WAIT near-signal outcomes."""
from __future__ import annotations

import json
import os
import tempfile
import time
import unittest
from pathlib import Path

from keel.config import refresh_settings
from keel.domain.records import FactorSnapshot
from keel.exchange.okx_fees import clear_fee_caches
from keel.ledger import KeelLedger
from keel.ledger.near_entry_markout import (
    compute_near_entry_markout,
    is_near_wait_diag,
    lookup_entry_price,
    signal_diag_from_calculus,
)
from keel.ledger.shadow_markout import markout_bps


class TestNearWaitDiag(unittest.TestCase):
    def test_accepts_1_2_missing(self):
        self.assertTrue(
            is_near_wait_diag({"nearest": "long", "missing": ["volume_ok"]})
        )
        self.assertTrue(
            is_near_wait_diag(
                {"nearest": "short", "missing": ["rsi_short_ok", "volume_ok"]}
            )
        )

    def test_rejects_zero_or_three_or_none(self):
        self.assertFalse(is_near_wait_diag({"nearest": "long", "missing": []}))
        self.assertFalse(
            is_near_wait_diag(
                {
                    "nearest": "long",
                    "missing": ["a", "b", "c"],
                }
            )
        )
        self.assertFalse(is_near_wait_diag({"nearest": "none", "missing": ["a"]}))
        self.assertFalse(is_near_wait_diag(None))

    def test_signal_diag_from_calculus_json(self):
        raw = json.dumps(
            {"market_source": "okx_public", "signal_diag": {"nearest": "short"}}
        )
        self.assertEqual(signal_diag_from_calculus(raw).get("nearest"), "short")


class TestNearEntryMarkoutLedger(unittest.TestCase):
    def setUp(self):
        clear_fee_caches()
        self.temp = tempfile.TemporaryDirectory()
        self.db = Path(self.temp.name) / "near_entry.db"
        os.environ["KEEL_LEDGER_DB"] = str(self.db)
        for k in (
            "KEEL_SHADOW_FEE_ROLE",
            "KEEL_SHADOW_MAKER_FEE_BPS",
            "KEEL_SHADOW_TAKER_FEE_BPS",
            "KEEL_OKX_API_KEY",
            "KEEL_OKX_SECRET_KEY",
            "KEEL_OKX_PASSPHRASE",
        ):
            os.environ.pop(k, None)
        refresh_settings()
        self.ledger = KeelLedger(self.db)
        self.t0 = time.time() - 7200

    def tearDown(self):
        clear_fee_caches()
        self.ledger.close()
        os.environ.pop("KEEL_LEDGER_DB", None)
        for k in (
            "KEEL_SHADOW_FEE_ROLE",
            "KEEL_SHADOW_MAKER_FEE_BPS",
            "KEEL_SHADOW_TAKER_FEE_BPS",
        ):
            os.environ.pop(k, None)
        refresh_settings()
        self.temp.cleanup()

    def _seed_wait(
        self,
        *,
        ts: float,
        inst_id: str,
        nearest: str,
        missing: list[str],
        market_source: str = "okx_public",
        price: float | None = 100.0,
        entry_price: float | None = None,
    ) -> None:
        from keel.domain.records import DecisionRecord

        calc = {
            "market_source": market_source,
            "signal_diag": {
                "nearest": nearest,
                "missing": missing,
                "edge_hint_bps": 0.0,
            },
        }
        self.ledger.record_decision(
            DecisionRecord(
                timestamp=ts,
                inst_id=inst_id,
                action="WAIT",
                confidence=0.5,
                entry_price=entry_price,
                reason="test wait near",
                calculus_data=calc,
                policy_name="rule",
            )
        )
        if price is not None and price > 0:
            self.ledger.record_factor_snapshot(
                FactorSnapshot(
                    timestamp=ts,
                    inst_id=inst_id,
                    price=price,
                )
            )

    def _seed_later(self, *, ts: float, price: float, inst_id: str) -> None:
        self.ledger.record_factor_snapshot(
            FactorSnapshot(timestamp=ts, inst_id=inst_id, price=price)
        )

    def test_empty(self):
        raw = compute_near_entry_markout(
            self.ledger._get_conn(),
            hours=24.0,
            horizons=(60, 300),
            apply_funding=False,
        )
        self.assertEqual(raw["count"], 0)
        self.assertIn("fee_model", raw)
        self.assertTrue(raw["recommend_only"])
        self.assertIsNone(raw["frac_clear_net_rt_hurdle"])

    def test_long_near_gross_and_net(self):
        # 1-missing long WAIT @100 → later 101 (+100bps gross); taker RT 10 → net 90
        self._seed_wait(
            ts=self.t0,
            inst_id="BTC-USDT-SWAP",
            nearest="long",
            missing=["volume_ok"],
            price=100.0,
        )
        self._seed_later(ts=self.t0 + 65, price=101.0, inst_id="BTC-USDT-SWAP")
        self._seed_later(ts=self.t0 + 310, price=101.0, inst_id="BTC-USDT-SWAP")

        raw = compute_near_entry_markout(
            self.ledger._get_conn(),
            hours=24.0,
            horizons=(60, 300),
            apply_funding=False,
        )
        self.assertEqual(raw["count"], 1)
        self.assertEqual(raw["by_nearest"].get("long"), 1)
        self.assertEqual(raw["by_action"].get("BUY_LONG"), 1)
        self.assertAlmostEqual(raw["fee_model"]["round_trip_fee_bps"], 10.0)

        horizons = {h["horizon_seconds"]: h for h in raw["markout"]["horizons"]}
        h60 = horizons[60]
        self.assertEqual(h60["sample_count"], 1)
        self.assertAlmostEqual(h60["avg_markout_bps"], 100.0, places=4)
        self.assertAlmostEqual(h60["avg_net_roundtrip_markout_bps"], 90.0, places=4)
        self.assertEqual(h60["win_rate_net_roundtrip"], 1.0)
        self.assertEqual(h60["frac_clear_net_rt_hurdle"], 1.0)

        h300 = horizons[300]
        self.assertEqual(h300["sample_count"], 1)
        self.assertEqual(raw["frac_clear_net_rt_hurdle"], 1.0)
        self.assertEqual(raw["frac_clear_10bps_net_at_300s"], 1.0)

        by_inst = raw["by_instrument"]["BTC-USDT-SWAP"]
        self.assertEqual(by_inst["count"], 1)
        self.assertAlmostEqual(
            by_inst["markout_300s"]["avg_net_roundtrip_markout_bps"], 90.0, places=4
        )

    def test_short_near_and_excludes_3_missing(self):
        self._seed_wait(
            ts=self.t0,
            inst_id="ETH-USDT-SWAP",
            nearest="short",
            missing=["rsi_short_ok", "volume_ok"],
            price=200.0,
        )
        self._seed_later(ts=self.t0 + 70, price=198.0, inst_id="ETH-USDT-SWAP")
        # 3-missing should be ignored
        self._seed_wait(
            ts=self.t0 + 10,
            inst_id="ETH-USDT-SWAP",
            nearest="short",
            missing=["a", "b", "c"],
            price=200.0,
        )
        self._seed_later(ts=self.t0 + 80, price=190.0, inst_id="ETH-USDT-SWAP")

        raw = compute_near_entry_markout(
            self.ledger._get_conn(),
            hours=24.0,
            horizons=(60,),
            apply_funding=False,
        )
        self.assertEqual(raw["count"], 1)
        self.assertEqual(raw["by_nearest"].get("short"), 1)
        h = raw["markout"]["horizons"][0]
        self.assertAlmostEqual(h["avg_markout_bps"], 100.0, places=4)
        self.assertAlmostEqual(h["avg_net_roundtrip_markout_bps"], 90.0, places=4)

    def test_market_source_and_inst_filter(self):
        self._seed_wait(
            ts=self.t0,
            inst_id="BTC-USDT-SWAP",
            nearest="long",
            missing=["volume_ok"],
            market_source="okx_public",
            price=100.0,
        )
        self._seed_later(ts=self.t0 + 70, price=101.0, inst_id="BTC-USDT-SWAP")
        self._seed_wait(
            ts=self.t0 + 5,
            inst_id="SOL-USDT-SWAP",
            nearest="long",
            missing=["volume_ok"],
            market_source="synthetic",
            price=50.0,
        )
        self._seed_later(ts=self.t0 + 75, price=51.0, inst_id="SOL-USDT-SWAP")

        only_okx = compute_near_entry_markout(
            self.ledger._get_conn(),
            hours=24.0,
            horizons=(60,),
            market_source="okx_public",
            apply_funding=False,
        )
        self.assertEqual(only_okx["count"], 1)
        self.assertIn("BTC-USDT-SWAP", only_okx["by_instrument"])
        self.assertNotIn("SOL-USDT-SWAP", only_okx["by_instrument"])

        only_sol = compute_near_entry_markout(
            self.ledger._get_conn(),
            hours=24.0,
            horizons=(60,),
            inst_id="SOL-USDT-SWAP",
            apply_funding=False,
        )
        self.assertEqual(only_sol["count"], 1)
        self.assertIn("SOL-USDT-SWAP", only_sol["by_instrument"])

    def test_skip_when_no_later_price(self):
        self._seed_wait(
            ts=self.t0,
            inst_id="BTC-USDT-SWAP",
            nearest="long",
            missing=["volume_ok"],
            price=100.0,
        )
        raw = compute_near_entry_markout(
            self.ledger._get_conn(),
            hours=24.0,
            horizons=(60, 300),
            apply_funding=False,
        )
        self.assertEqual(raw["count"], 1)
        for h in raw["markout"]["horizons"]:
            self.assertEqual(h["sample_count"], 0)
            self.assertEqual(h["skipped"], 1)

    def test_lookup_entry_price_fallback(self):
        conn = self.ledger._get_conn()
        ts = self.t0
        # No exact match; nearby within 30s
        self.ledger.record_factor_snapshot(
            FactorSnapshot(timestamp=ts + 5.0, inst_id="BTC-USDT-SWAP", price=111.0)
        )
        found = lookup_entry_price(
            conn, inst_id="BTC-USDT-SWAP", decision_ts=ts, entry_price=None
        )
        self.assertIsNotNone(found)
        assert found is not None
        self.assertAlmostEqual(found[0], 111.0)
        self.assertEqual(found[1], "factor_snapshots_near")

    def test_below_hurdle_fraction(self):
        # Tiny favorable move: +5 bps gross → net RT -5 → does not clear 10
        self._seed_wait(
            ts=self.t0,
            inst_id="BTC-USDT-SWAP",
            nearest="long",
            missing=["volume_ok"],
            price=10000.0,
        )
        # +5 bps = 10000 * 1.0005 = 10005
        self._seed_later(ts=self.t0 + 310, price=10005.0, inst_id="BTC-USDT-SWAP")
        raw = compute_near_entry_markout(
            self.ledger._get_conn(),
            hours=24.0,
            horizons=(300,),
            apply_funding=False,
        )
        h = raw["markout"]["horizons"][0]
        self.assertAlmostEqual(h["avg_markout_bps"], 5.0, places=4)
        self.assertAlmostEqual(h["avg_net_roundtrip_markout_bps"], -5.0, places=4)
        self.assertEqual(h["frac_clear_net_rt_hurdle"], 0.0)
        self.assertEqual(raw["frac_clear_10bps_net_at_300s"], 0.0)
        # Sanity: markout helper agrees
        self.assertAlmostEqual(markout_bps("BUY_LONG", 10000.0, 10005.0), 5.0, places=4)

    def test_cli_runs(self):
        import subprocess
        import sys

        self._seed_wait(
            ts=self.t0,
            inst_id="BTC-USDT-SWAP",
            nearest="long",
            missing=["volume_ok"],
            price=100.0,
        )
        self._seed_later(ts=self.t0 + 70, price=101.0, inst_id="BTC-USDT-SWAP")
        repo = Path(__file__).resolve().parents[1]
        out = Path(self.temp.name) / "out.json"
        env = os.environ.copy()
        env["PYTHONPATH"] = str(repo)
        proc = subprocess.run(
            [
                sys.executable,
                str(repo / "scripts" / "near_entry_markout.py"),
                "--db",
                str(self.db),
                "--hours",
                "24",
                "--horizons",
                "60",
                "--no-funding",
                "--json-only",
                "--out",
                str(out),
            ],
            cwd=str(repo),
            env=env,
            capture_output=True,
            text=True,
        )
        self.assertEqual(proc.returncode, 0, proc.stderr)
        body = json.loads(out.read_text(encoding="utf-8"))
        self.assertEqual(body["count"], 1)
        self.assertTrue(body["recommend_only"])


if __name__ == "__main__":
    unittest.main()
