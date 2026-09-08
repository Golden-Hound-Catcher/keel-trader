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
    COHORT_POST_E31,
    COHORT_PRE_E31,
    aggregate_full_gate_fires,
    classify_full_gate_cohort,
    compute_full_gate_markout,
    is_full_gate_fire,
    is_post_e31_signal_diag,
    missing_list,
    summarize_full_gate_markout_primary,
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


class TestFullGateCohortClassify(unittest.TestCase):
    def test_post_e31_via_require_4h_trend(self):
        diag = {
            "nearest": "short",
            "missing": [],
            "require_4h_trend": True,
            "trend_gate": "15m+1h+4h",
        }
        self.assertTrue(is_post_e31_signal_diag(diag))
        self.assertEqual(classify_full_gate_cohort(diag), COHORT_POST_E31)

    def test_post_e31_via_trend_gate_4h_only(self):
        diag = {
            "nearest": "long",
            "missing": [],
            "require_4h_trend": False,
            "trend_gate": "15m+1h+4h",
        }
        self.assertTrue(is_post_e31_signal_diag(diag))
        self.assertEqual(classify_full_gate_cohort(diag), COHORT_POST_E31)

    def test_pre_e31_without_4h(self):
        diag = {
            "nearest": "short",
            "missing": [],
            "require_4h_trend": False,
            "trend_gate": "15m+1h",
            "trend_4h": "neutral",
        }
        self.assertFalse(is_post_e31_signal_diag(diag))
        self.assertEqual(classify_full_gate_cohort(diag), COHORT_PRE_E31)

    def test_truthy_require_string_and_int(self):
        self.assertEqual(
            classify_full_gate_cohort(
                {"nearest": "long", "missing": [], "require_4h_trend": "1"}
            ),
            COHORT_POST_E31,
        )
        self.assertEqual(
            classify_full_gate_cohort(
                {"nearest": "long", "missing": [], "require_4h_trend": 1}
            ),
            COHORT_POST_E31,
        )
        self.assertEqual(
            classify_full_gate_cohort(
                {"nearest": "long", "missing": [], "trend_gate": "15m"}
            ),
            COHORT_PRE_E31,
        )


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
        require_4h_trend: bool | None = None,
        trend_gate: str | None = None,
        diag_extra: dict | None = None,
    ) -> None:
        calc: dict = {"market_source": "okx_public"}
        if with_diag:
            nearest = "long" if action == "BUY_LONG" else "short"
            diag = {
                "nearest": nearest,
                "missing": [] if missing is None else missing,
            }
            if require_4h_trend is not None:
                diag["require_4h_trend"] = require_4h_trend
            if trend_gate is not None:
                diag["trend_gate"] = trend_gate
            if diag_extra:
                diag.update(diag_extra)
            calc["signal_diag"] = diag
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
        self._seed_fire(
            ts=now - 100,
            inst_id="BTC-USDT-SWAP",
            action="BUY_LONG",
            require_4h_trend=True,
            trend_gate="15m+1h+4h",
        )
        self._seed_fire(
            ts=now - 90,
            inst_id="ETH-USDT-SWAP",
            action="SELL_SHORT",
            price=200.0,
            entry_price=200.0,
            require_4h_trend=True,
            trend_gate="15m+1h+4h",
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
        self.assertIn(
            body.get("economic_evidence"),
            ("full_gate", "none", "mixed", "probe", "stale_pre_e31"),
        )
        self.assertEqual(body["economic_evidence"], "full_gate")
        self.assertEqual(fg["by_cohort"]["post_e31"]["count"], 2)
        self.assertEqual(fg["by_cohort"]["pre_e31"]["count"], 0)
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
        self._seed_fire(
            ts=ts,
            inst_id=inst,
            action="BUY_LONG",
            price=100.0,
            require_4h_trend=True,
            trend_gate="15m+1h+4h",
        )
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
            ts=ts,
            inst_id=inst,
            action="SELL_SHORT",
            price=200.0,
            entry_price=200.0,
            require_4h_trend=True,
            trend_gate="15m+1h+4h",
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
        self._seed_fire(
            ts=ts,
            inst_id=inst,
            action="BUY_LONG",
            price=100.0,
            require_4h_trend=True,
            trend_gate="15m+1h+4h",
        )
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

    def test_f1_cohort_split_quality_and_markout(self):
        """F1: mixed pre/post ledger — primary markout prefers post_e31."""
        ts = self.t0
        # 3 pre-e31 shorts (stale spray) — losing markout
        for i in range(3):
            t = ts + i * 10
            self._seed_fire(
                ts=t,
                inst_id="BTC-USDT-SWAP",
                action="SELL_SHORT",
                price=100.0,
                entry_price=100.0,
                require_4h_trend=False,
                trend_gate="15m+1h",
            )
            self.ledger.record_factor_snapshot(
                FactorSnapshot(
                    timestamp=t + 305,
                    inst_id="BTC-USDT-SWAP",
                    price=101.0,  # short loses
                    ema_9=101.0,
                    ema_21=101.0,
                    rsi_14=50.0,
                    atr_14=1.0,
                    volume_ratio=1.0,
                )
            )
        # 1 post-e31 long — winning
        t_post = ts + 100
        self._seed_fire(
            ts=t_post,
            inst_id="ETH-USDT-SWAP",
            action="BUY_LONG",
            price=200.0,
            entry_price=200.0,
            require_4h_trend=True,
            trend_gate="15m+1h+4h",
        )
        self.ledger.record_factor_snapshot(
            FactorSnapshot(
                timestamp=t_post + 305,
                inst_id="ETH-USDT-SWAP",
                price=202.0,
                ema_9=202.0,
                ema_21=202.0,
                rsi_14=50.0,
                atr_14=1.0,
                volume_ratio=1.0,
            )
        )

        agg = aggregate_full_gate_fires(self.ledger._get_conn(), since=ts - 10)
        self.assertEqual(agg["count"], 4)
        self.assertEqual(agg["by_cohort"][COHORT_PRE_E31]["count"], 3)
        self.assertEqual(agg["by_cohort"][COHORT_POST_E31]["count"], 1)

        post = compute_full_gate_markout(
            self.ledger._get_conn(),
            hours=24.0,
            now=ts + 2000,
            apply_funding=False,
            horizons=(300,),
            cohort=COHORT_POST_E31,
        )
        pre = compute_full_gate_markout(
            self.ledger._get_conn(),
            hours=24.0,
            now=ts + 2000,
            apply_funding=False,
            horizons=(300,),
            cohort=COHORT_PRE_E31,
        )
        self.assertEqual(post["count"], 1)
        self.assertEqual(pre["count"], 3)
        primary, pre_sum = summarize_full_gate_markout_primary(post, pre)
        self.assertEqual(primary["cohort"], COHORT_POST_E31)
        self.assertEqual(primary["count"], 1)
        self.assertGreaterEqual(int(primary.get("sample_count") or 0), 1)
        self.assertIsNotNone(primary.get("win_rate_net_roundtrip"))
        self.assertEqual(pre_sum["count"], 3)

        r = self.client.get("/api/v1/stats/quality?hours=24")
        self.assertEqual(r.status_code, 200)
        body = r.json()
        fg = body["full_gate_fires"]
        self.assertEqual(fg["count"], 4)
        self.assertEqual(fg["by_cohort"]["post_e31"]["count"], 1)
        self.assertEqual(fg["by_cohort"]["pre_e31"]["count"], 3)
        mo = body["full_gate_markout"]
        self.assertEqual(mo["cohort"], "post_e31")
        self.assertEqual(mo["count"], 1)
        self.assertIsNotNone(mo.get("win_rate_net_roundtrip"))
        pre_mo = body["full_gate_markout_pre_e31"]
        self.assertEqual(pre_mo["cohort"], "pre_e31")
        self.assertEqual(pre_mo["count"], 3)
        self.assertEqual(body["economic_evidence"], "full_gate")

    def test_f1_pre_only_does_not_poison_primary(self):
        ts = self.t0
        self._seed_fire(
            ts=ts,
            inst_id="BTC-USDT-SWAP",
            action="SELL_SHORT",
            require_4h_trend=False,
            trend_gate="15m+1h",
        )
        self.ledger.record_factor_snapshot(
            FactorSnapshot(
                timestamp=ts + 305,
                inst_id="BTC-USDT-SWAP",
                price=101.0,
                ema_9=101.0,
                ema_21=101.0,
                rsi_14=50.0,
                atr_14=1.0,
                volume_ratio=1.0,
            )
        )
        r = self.client.get("/api/v1/stats/quality?hours=24")
        self.assertEqual(r.status_code, 200)
        body = r.json()
        self.assertEqual(body["economic_evidence"], "stale_pre_e31")
        mo = body["full_gate_markout"]
        self.assertEqual(mo["cohort"], "post_e31")
        self.assertEqual(mo["count"], 0)
        self.assertIsNone(mo.get("win_rate_net_roundtrip"))
        self.assertTrue(mo.get("stale_pre_e31_note"))
        self.assertEqual(body["full_gate_markout_pre_e31"]["count"], 1)


if __name__ == "__main__":
    unittest.main()
