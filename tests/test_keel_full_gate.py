"""E1 full-gate fire detection, quality stats, and markout."""
from __future__ import annotations

import json
import os
import tempfile
import time
import unittest
from pathlib import Path

from fastapi.testclient import TestClient

from keel.api.app import create_app
from keel.api.deps import set_ledger_path_override
from keel.config import refresh_settings
from keel.domain.records import DecisionRecord, FactorSnapshot
from keel.exchange.okx_fees import clear_fee_caches
from keel.ledger import KeelLedger
from keel.ledger.full_gate import (
    aggregate_full_gate_fires,
    compute_full_gate_markout,
    is_full_gate_fire,
    missing_list,
)


class TestFullGateDetect(unittest.TestCase):
    def test_accepts_empty_missing_buy_sell(self):
        self.assertTrue(
            is_full_gate_fire(
                "BUY_LONG",
                {"nearest": "long", "missing": []},
                policy_name="rule",
            )
        )
        self.assertTrue(
            is_full_gate_fire(
                "SELL_SHORT",
                {"nearest": "short", "missing": []},
                policy_name="rule",
            )
        )
        # missing key absent but nearest present → treat as empty
        self.assertTrue(
            is_full_gate_fire(
                "BUY_LONG",
                {"nearest": "long"},
                policy_name="rule",
            )
        )

    def test_rejects_wait_near_forced_llm(self):
        self.assertFalse(
            is_full_gate_fire(
                "WAIT",
                {"nearest": "long", "missing": []},
                policy_name="rule",
            )
        )
        self.assertFalse(
            is_full_gate_fire(
                "BUY_LONG",
                {"nearest": "long", "missing": ["volume_ok"]},
                policy_name="rule",
            )
        )
        # forced paper: no signal_diag
        self.assertFalse(is_full_gate_fire("BUY_LONG", None, policy_name="rule"))
        self.assertFalse(is_full_gate_fire("BUY_LONG", {}, policy_name="rule"))
        self.assertFalse(
            is_full_gate_fire(
                "BUY_LONG",
                {"nearest": "long", "missing": []},
                policy_name="llm",
            )
        )

    def test_missing_list_helper(self):
        self.assertEqual(missing_list({"nearest": "long", "missing": []}), [])
        self.assertIsNone(missing_list(None))
        self.assertIsNone(missing_list({"rsi_14": 50}))


class TestFullGateQualityAndMarkout(unittest.TestCase):
    def setUp(self):
        clear_fee_caches()
        self.temp = tempfile.TemporaryDirectory()
        self.db = Path(self.temp.name) / "full_gate.db"
        set_ledger_path_override(self.db)
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
        self.client = TestClient(create_app())
        self.t0 = time.time() - 7200

    def tearDown(self):
        clear_fee_caches()
        self.ledger.close()
        set_ledger_path_override(None)
        os.environ.pop("KEEL_LEDGER_DB", None)
        for k in (
            "KEEL_SHADOW_FEE_ROLE",
            "KEEL_SHADOW_MAKER_FEE_BPS",
            "KEEL_SHADOW_TAKER_FEE_BPS",
        ):
            os.environ.pop(k, None)
        refresh_settings()
        self.temp.cleanup()

    def _seed_fire(
        self,
        *,
        ts: float,
        inst_id: str,
        action: str = "BUY_LONG",
        missing: list | None = None,
        policy_name: str = "rule",
        price: float = 100.0,
        entry_price: float | None = 100.0,
        with_diag: bool = True,
    ) -> None:
        calc: dict = {"market_source": "okx_public"}
        if with_diag:
            nearest = "long" if action == "BUY_LONG" else "short"
            calc["signal_diag"] = {
                "nearest": nearest,
                "missing": [] if missing is None else missing,
            }
        self.ledger.record_decision(
            DecisionRecord(
                timestamp=ts,
                inst_id=inst_id,
                action=action,
                confidence=70.0,
                entry_price=entry_price,
                reason="test full gate",
                calculus_data=calc,
                policy_name=policy_name,
            )
        )
        if price and price > 0:
            self.ledger.record_factor_snapshot(
                FactorSnapshot(
                    timestamp=ts,
                    inst_id=inst_id,
                    price=price,
                    ema_9=price,
                    ema_21=price,
                    rsi_14=50.0,
                    atr_14=1.0,
                    volume_ratio=1.0,
                )
            )

    def test_quality_api_full_gate_fires(self):
        now = time.time()
        self._seed_fire(ts=now - 100, inst_id="BTC-USDT-SWAP", action="BUY_LONG")
        self._seed_fire(
            ts=now - 90,
            inst_id="ETH-USDT-SWAP",
            action="SELL_SHORT",
            price=200.0,
            entry_price=200.0,
        )
        # near WAIT — not a full-gate fire
        self.ledger.record_decision(
            DecisionRecord(
                timestamp=now - 80,
                inst_id="BTC-USDT-SWAP",
                action="WAIT",
                confidence=40.0,
                reason="near",
                policy_name="rule",
                calculus_data={
                    "market_source": "okx_public",
                    "signal_diag": {"nearest": "long", "missing": ["volume_ok"]},
                },
            )
        )
        # forced paper without signal_diag — excluded
        self._seed_fire(
            ts=now - 70,
            inst_id="SOL-USDT-SWAP",
            action="BUY_LONG",
            with_diag=False,
        )
        # missing gates on BUY — excluded
        self._seed_fire(
            ts=now - 60,
            inst_id="XRP-USDT-SWAP",
            action="BUY_LONG",
            missing=["rsi_long_ok"],
        )

        r = self.client.get("/api/v1/stats/quality?hours=24")
        self.assertEqual(r.status_code, 200)
        body = r.json()
        fg = body["full_gate_fires"]
        self.assertEqual(fg["count"], 2)
        self.assertEqual(fg["by_action"].get("BUY_LONG"), 1)
        self.assertEqual(fg["by_action"].get("SELL_SHORT"), 1)
        self.assertEqual(fg["by_instrument"].get("BTC-USDT-SWAP"), 1)
        self.assertEqual(fg["by_instrument"].get("ETH-USDT-SWAP"), 1)
        self.assertIn(body.get("economic_evidence"), ("full_gate", "none", "mixed", "probe"))
        self.assertEqual(body["economic_evidence"], "full_gate")
        btc = (body.get("by_instrument") or {}).get("BTC-USDT-SWAP") or {}
        self.assertEqual(btc.get("full_gate_fires"), 1)

        direct = self.ledger.get_quality_stats(hours=24.0)
        self.assertEqual(direct["full_gate_fires"]["count"], 2)

        agg = aggregate_full_gate_fires(
            self.ledger._get_conn(), since=now - 3600
        )
        self.assertEqual(agg["count"], 2)

    def test_full_gate_markout_counterfactual(self):
        ts = self.t0
        inst = "BTC-USDT-SWAP"
        self._seed_fire(ts=ts, inst_id=inst, action="BUY_LONG", price=100.0)
        # later prices for 60/300/900
        for h, px in ((60, 100.5), (300, 101.0), (900, 101.2)):
            self.ledger.record_factor_snapshot(
                FactorSnapshot(
                    timestamp=ts + h + 5,
                    inst_id=inst,
                    price=px,
                    ema_9=px,
                    ema_21=px,
                    rsi_14=50.0,
                    atr_14=1.0,
                    volume_ratio=1.0,
                )
            )
        result = compute_full_gate_markout(
            self.ledger._get_conn(),
            hours=24.0,
            now=ts + 2000,
            apply_funding=False,
            settings=None,
        )
        self.assertEqual(result["count"], 1)
        self.assertEqual(result["cohort"], "full_gate")
        horizons = {h["horizon_seconds"]: h for h in result["markout"]["horizons"]}
        self.assertGreaterEqual(horizons[300]["sample_count"], 1)
        self.assertIsNotNone(horizons[300]["avg_net_roundtrip_markout_bps"])
        # gross ~100 bps at 300s; net RT ≈ gross - 10
        self.assertGreater(horizons[300]["avg_markout_bps"], 50)

    def test_full_gate_markout_prefers_shadow_fill(self):
        ts = self.t0
        inst = "ETH-USDT-SWAP"
        self._seed_fire(
            ts=ts, inst_id=inst, action="SELL_SHORT", price=200.0, entry_price=200.0
        )
        self.ledger.record_event(
            "shadow_fill",
            inst_id=inst,
            data={
                "action": "SELL_SHORT",
                "price": 200.0,
                "shadow": True,
                "policy": "rule",
                "probe": False,
            },
            timestamp=ts + 1,
        )
        self.ledger.record_factor_snapshot(
            FactorSnapshot(
                timestamp=ts + 305,
                inst_id=inst,
                price=198.0,  # short profits
                ema_9=198.0,
                ema_21=198.0,
                rsi_14=50.0,
                atr_14=1.0,
                volume_ratio=1.0,
            )
        )
        result = compute_full_gate_markout(
            self.ledger._get_conn(),
            hours=24.0,
            now=ts + 2000,
            apply_funding=False,
            horizons=(300,),
        )
        self.assertEqual(result["count"], 1)
        sources = result.get("entry_sources") or {}
        self.assertTrue(any("shadow_fill" in k for k in sources))

    def test_quality_api_full_gate_markout(self):
        """E3: quality scorecard includes full_gate_markout fields."""
        ts = self.t0
        inst = "BTC-USDT-SWAP"
        self._seed_fire(ts=ts, inst_id=inst, action="BUY_LONG", price=100.0)
        for h, px in ((60, 100.4), (300, 101.0), (900, 101.5)):
            self.ledger.record_factor_snapshot(
                FactorSnapshot(
                    timestamp=ts + h + 5,
                    inst_id=inst,
                    price=px,
                    ema_9=px,
                    ema_21=px,
                    rsi_14=50.0,
                    atr_14=1.0,
                    volume_ratio=1.0,
                )
            )
        r = self.client.get("/api/v1/stats/quality?hours=24")
        self.assertEqual(r.status_code, 200)
        body = r.json()
        self.assertIn("full_gate_markout", body)
        mo = body["full_gate_markout"]
        self.assertIsInstance(mo, dict)
        for key in (
            "sample_count",
            "win_rate_net_roundtrip",
            "avg_net_roundtrip_markout_bps",
            "frac_clear_net_rt_hurdle",
            "horizons",
        ):
            self.assertIn(key, mo)
        self.assertIsInstance(mo["horizons"], list)
        hs = {int(h["horizon_seconds"]) for h in mo["horizons"]}
        self.assertIn(300, hs)
        self.assertGreaterEqual(int(mo.get("sample_count") or 0), 1)
        direct = self.ledger.get_quality_stats(hours=24.0)
        self.assertIn("full_gate_markout", direct)
        self.assertIn("sample_count", direct["full_gate_markout"])



if __name__ == "__main__":
    unittest.main()
