"""Q3.2 shadow fill markout — offline outcome stats from ledger."""
from __future__ import annotations

import os
import tempfile
import time
import unittest
from pathlib import Path

from fastapi.testclient import TestClient

from keel.api.app import create_app
from keel.api.deps import set_ledger_path_override
from keel.config import refresh_settings
from keel.domain.records import FactorSnapshot
from keel.ledger import KeelLedger
from keel.ledger.shadow_markout import (
    DEFAULT_MARKOUT_HORIZONS_SECONDS,
    compute_shadow_markout,
    markout_bps,
)


class TestMarkoutBps(unittest.TestCase):
    def test_long_positive(self):
        self.assertAlmostEqual(markout_bps("BUY_LONG", 100.0, 101.0), 100.0)

    def test_short_positive(self):
        self.assertAlmostEqual(markout_bps("SELL_SHORT", 100.0, 99.0), 100.0)

    def test_invalid(self):
        self.assertIsNone(markout_bps("BUY_LONG", 0, 100))
        self.assertIsNone(markout_bps("WAIT", 100, 101))


class TestShadowMarkoutLedger(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.db = Path(self.temp.name) / "markout.db"
        set_ledger_path_override(self.db)
        os.environ["KEEL_LEDGER_DB"] = str(self.db)
        refresh_settings()
        self.ledger = KeelLedger(self.db)
        self.client = TestClient(create_app())
        self.t0 = time.time() - 3600

    def tearDown(self):
        self.ledger.close()
        set_ledger_path_override(None)
        os.environ.pop("KEEL_LEDGER_DB", None)
        refresh_settings()
        self.temp.cleanup()

    def _seed_fill(
        self,
        *,
        ts: float,
        action: str,
        price: float,
        probe: bool = False,
        inst_id: str = "BTC-USDT-SWAP",
    ) -> None:
        data = {"action": action, "price": price, "shadow": True}
        if probe:
            data["policy"] = "shadow_near_probe"
            data["probe"] = True
        self.ledger.record_event(
            "shadow_fill",
            inst_id=inst_id,
            data=data,
            timestamp=ts,
        )

    def _seed_factor(self, *, ts: float, price: float, inst_id: str = "BTC-USDT-SWAP") -> None:
        self.ledger.record_factor_snapshot(
            FactorSnapshot(
                timestamp=ts,
                inst_id=inst_id,
                price=price,
            )
        )

    def test_empty_api(self):
        r = self.client.get("/api/v1/stats/shadow?hours=24")
        self.assertEqual(r.status_code, 200)
        body = r.json()
        self.assertEqual(body["count"], 0)
        self.assertIn("markout", body)
        self.assertEqual(body["markout"]["horizons"][0]["sample_count"], 0)

    def test_markout_long_and_probe(self):
        # Long fill at t0, later price up → positive markout.
        self._seed_fill(ts=self.t0, action="BUY_LONG", price=100.0, probe=False)
        self._seed_factor(ts=self.t0 + 65, price=101.0)  # ~60s
        self._seed_factor(ts=self.t0 + 310, price=102.0)  # ~300s
        self._seed_factor(ts=self.t0 + 910, price=103.0)  # ~900s

        # Probe short at t0+100, later price down → positive for short.
        self._seed_fill(
            ts=self.t0 + 100,
            action="SELL_SHORT",
            price=200.0,
            probe=True,
        )
        self._seed_factor(ts=self.t0 + 100 + 65, price=198.0)
        self._seed_factor(ts=self.t0 + 100 + 310, price=196.0)
        self._seed_factor(ts=self.t0 + 100 + 910, price=194.0)

        r = self.client.get("/api/v1/stats/shadow?hours=24")
        self.assertEqual(r.status_code, 200)
        body = r.json()
        self.assertEqual(body["count"], 2)
        self.assertEqual(body["probe_count"], 1)
        self.assertEqual(body["by_policy"].get("shadow_near_probe"), 1)

        horizons = {h["horizon_seconds"]: h for h in body["markout"]["horizons"]}
        self.assertIn(60, horizons)
        h60 = horizons[60]
        self.assertEqual(h60["sample_count"], 2)
        self.assertEqual(h60["skipped"], 0)
        self.assertIsNotNone(h60["avg_markout_bps"])
        self.assertGreater(h60["avg_markout_bps"], 0)
        self.assertEqual(h60["win_rate"], 1.0)
        self.assertEqual(h60["probe_sample_count"], 1)
        self.assertAlmostEqual(h60["probe_avg_markout_bps"], 100.0, places=4)
        self.assertEqual(h60["probe_win_rate"], 1.0)
        self.assertIn("BUY_LONG", h60["by_action"])
        self.assertIn("SELL_SHORT", h60["by_action"])

        # Sibling endpoint
        r2 = self.client.get("/api/v1/stats/shadow_markout?hours=24")
        self.assertEqual(r2.status_code, 200)
        self.assertEqual(r2.json()["count"], 2)
        self.assertEqual(len(r2.json()["markout"]["horizons"]), len(DEFAULT_MARKOUT_HORIZONS_SECONDS))

    def test_skip_when_no_later_price(self):
        self._seed_fill(ts=self.t0, action="BUY_LONG", price=100.0)
        # No factor snapshots → all skipped
        raw = self.ledger.get_shadow_markout(hours=24)
        for h in raw["markout"]["horizons"]:
            self.assertEqual(h["sample_count"], 0)
            self.assertEqual(h["skipped"], 1)

    def test_direct_helper_matches_ledger(self):
        self._seed_fill(ts=self.t0, action="BUY_LONG", price=50.0, probe=True)
        self._seed_factor(ts=self.t0 + 70, price=50.5)
        a = compute_shadow_markout(self.ledger._get_conn(), hours=24.0, horizons=(60,))
        b = self.ledger.get_shadow_stats(hours=24.0, markout_horizons=(60,))
        self.assertEqual(a["count"], b["count"])
        self.assertEqual(
            a["markout"]["horizons"][0]["sample_count"],
            b["markout"]["horizons"][0]["sample_count"],
        )
        self.assertAlmostEqual(
            a["markout"]["horizons"][0]["avg_markout_bps"],
            100.0,
            places=4,
        )


if __name__ == "__main__":
    unittest.main()
