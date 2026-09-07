"""Q3.2/Q3.3 shadow fill markout — offline outcome + OKX fee-aware nets."""
from __future__ import annotations

import json
import os
import tempfile
import time
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from fastapi.testclient import TestClient

from keel.api.app import create_app
from keel.api.deps import set_ledger_path_override
from keel.config import refresh_settings
from keel.domain.records import FactorSnapshot
from keel.exchange.okx_fees import (
    REGULAR_USDT_SWAP_MAKER_BPS,
    REGULAR_USDT_SWAP_TAKER_BPS,
    SwapFeeRates,
    build_fee_model,
    clear_fee_caches,
    crosses_standard_funding_boundary,
    get_swap_usdt_fee_rates,
    okx_rate_to_cost_bps,
)
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


class TestOkxFeeHelpers(unittest.TestCase):
    def setUp(self):
        clear_fee_caches()
        self._env_keys = [
            "KEEL_SHADOW_FEE_ROLE",
            "KEEL_SHADOW_MAKER_FEE_BPS",
            "KEEL_SHADOW_TAKER_FEE_BPS",
            "KEEL_OKX_API_KEY",
            "KEEL_OKX_SECRET_KEY",
            "KEEL_OKX_PASSPHRASE",
        ]
        self._prev = {k: os.environ.get(k) for k in self._env_keys}
        for k in self._env_keys:
            os.environ.pop(k, None)
        refresh_settings()

    def tearDown(self):
        clear_fee_caches()
        for k, v in self._prev.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v
        refresh_settings()

    def test_okx_rate_to_cost_bps(self):
        # OKX -0.0005 (pay) → +5 bps cost; +0.0002 (rebate) → -2 bps.
        self.assertAlmostEqual(okx_rate_to_cost_bps(-0.0005), 5.0)
        self.assertAlmostEqual(okx_rate_to_cost_bps(0.0002), -2.0)

    def test_defaults_regular_fallback(self):
        settings = refresh_settings()
        rates = get_swap_usdt_fee_rates(settings)
        self.assertEqual(rates.source, "fallback")
        self.assertAlmostEqual(rates.maker_bps, REGULAR_USDT_SWAP_MAKER_BPS)
        self.assertAlmostEqual(rates.taker_bps, REGULAR_USDT_SWAP_TAKER_BPS)
        model = build_fee_model(settings)
        self.assertEqual(model["role"], "taker")
        self.assertAlmostEqual(model["open_fee_bps"], 5.0)
        self.assertAlmostEqual(model["round_trip_fee_bps"], 10.0)
        self.assertEqual(model["inst_type"], "SWAP")
        self.assertEqual(model["margin"], "USDT")

    def test_taker_rt_10bps_maker_rt_4bps(self):
        os.environ["KEEL_SHADOW_FEE_ROLE"] = "taker"
        refresh_settings()
        m = build_fee_model(refresh_settings())
        self.assertAlmostEqual(m["round_trip_fee_bps"], 10.0)

        os.environ["KEEL_SHADOW_FEE_ROLE"] = "maker"
        refresh_settings()
        m2 = build_fee_model(refresh_settings())
        self.assertEqual(m2["role"], "maker")
        self.assertAlmostEqual(m2["open_fee_bps"], 2.0)
        self.assertAlmostEqual(m2["round_trip_fee_bps"], 4.0)

    def test_override_and_negative_maker_rebate(self):
        os.environ["KEEL_SHADOW_MAKER_FEE_BPS"] = "-1.0"
        os.environ["KEEL_SHADOW_TAKER_FEE_BPS"] = "5.0"
        os.environ["KEEL_SHADOW_FEE_ROLE"] = "maker"
        refresh_settings()
        m = build_fee_model(refresh_settings())
        self.assertEqual(m["source"], "override")
        self.assertAlmostEqual(m["maker_bps"], -1.0)
        self.assertAlmostEqual(m["open_fee_bps"], -1.0)
        self.assertAlmostEqual(m["round_trip_fee_bps"], -2.0)

    def test_live_makerU_takerU(self):
        def transport(method, url, headers, body):
            self.assertIn("/api/v5/account/trade-fee", url)
            self.assertIn("instType=SWAP", url)
            # OKX: negative = pay. makerU -0.0002 → 2bps; takerU -0.0005 → 5bps
            return json.dumps(
                {
                    "code": "0",
                    "data": [
                        {
                            "instType": "SWAP",
                            "level": "Lv1",
                            "maker": "-0.0002",
                            "taker": "-0.0005",
                            "makerU": "-0.00015",
                            "takerU": "-0.00045",
                        }
                    ],
                }
            )

        settings = SimpleNamespace(
            okx_api_key="k",
            okx_secret_key="s",
            okx_passphrase="p",
            okx_environment="demo",
            is_demo=True,
            okx_configured=True,
            shadow_fee_role="taker",
            shadow_maker_fee_bps=None,
            shadow_taker_fee_bps=None,
        )
        rates = get_swap_usdt_fee_rates(
            settings, transport=transport, force_refresh=True
        )
        self.assertEqual(rates.source, "live")
        self.assertAlmostEqual(rates.maker_bps, 1.5, places=4)
        self.assertAlmostEqual(rates.taker_bps, 4.5, places=4)
        # Must use U fields, not crypto-margined maker/taker (would be 2/5).
        self.assertNotAlmostEqual(rates.maker_bps, 2.0, places=4)

    def test_funding_boundary(self):
        # 07:59 → 08:01 UTC crosses 08:00
        fill = 8 * 3600 - 60
        end = 8 * 3600 + 60
        self.assertTrue(crosses_standard_funding_boundary(fill, end))
        # Short window inside hour: no cross
        self.assertFalse(crosses_standard_funding_boundary(100.0, 160.0))


class TestShadowMarkoutLedger(unittest.TestCase):
    def setUp(self):
        clear_fee_caches()
        self.temp = tempfile.TemporaryDirectory()
        self.db = Path(self.temp.name) / "markout.db"
        set_ledger_path_override(self.db)
        os.environ["KEEL_LEDGER_DB"] = str(self.db)
        for k in (
            "KEEL_SHADOW_FEE_ROLE",
            "KEEL_SHADOW_MAKER_FEE_BPS",
            "KEEL_SHADOW_TAKER_FEE_BPS",
        ):
            os.environ.pop(k, None)
        refresh_settings()
        self.ledger = KeelLedger(self.db)
        self.client = TestClient(create_app())
        self.t0 = time.time() - 3600

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
        self.assertIn("fee_model", body)
        self.assertEqual(body["markout"]["horizons"][0]["sample_count"], 0)
        fm = body["fee_model"]
        self.assertEqual(fm["role"], "taker")
        self.assertAlmostEqual(fm["round_trip_fee_bps"], 10.0)
        self.assertEqual(fm["margin"], "USDT")

    def test_markout_long_and_probe_gross_and_net(self):
        # Long fill at t0, later price up → +100 bps gross @60s
        self._seed_fill(ts=self.t0, action="BUY_LONG", price=100.0, probe=False)
        self._seed_factor(ts=self.t0 + 65, price=101.0)
        self._seed_factor(ts=self.t0 + 310, price=102.0)
        self._seed_factor(ts=self.t0 + 910, price=103.0)

        # Probe short at t0+100, later price down → +100 bps gross
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
        self.assertIn("fee_model", body)
        self.assertAlmostEqual(body["fee_model"]["open_fee_bps"], 5.0)
        self.assertAlmostEqual(body["fee_model"]["round_trip_fee_bps"], 10.0)

        horizons = {h["horizon_seconds"]: h for h in body["markout"]["horizons"]}
        h60 = horizons[60]
        self.assertEqual(h60["sample_count"], 2)
        self.assertEqual(h60["skipped"], 0)
        # Gross unchanged at +100 for probe short
        self.assertAlmostEqual(h60["probe_avg_markout_bps"], 100.0, places=4)
        # Net open = gross - 5; net RT = gross - 10
        self.assertAlmostEqual(h60["probe_avg_net_open_markout_bps"], 95.0, places=4)
        self.assertAlmostEqual(
            h60["probe_avg_net_roundtrip_markout_bps"], 90.0, places=4
        )
        self.assertEqual(h60["probe_win_rate_net_roundtrip"], 1.0)
        # Combined avg gross ~100; net RT ~90
        self.assertAlmostEqual(h60["avg_markout_bps"], 100.0, places=4)
        self.assertAlmostEqual(h60["avg_net_roundtrip_markout_bps"], 90.0, places=4)
        self.assertAlmostEqual(
            h60["avg_net_roundtrip_markout_bps"],
            h60["avg_markout_bps"] - body["fee_model"]["round_trip_fee_bps"],
            places=4,
        )

        r2 = self.client.get("/api/v1/stats/shadow_markout?hours=24")
        self.assertEqual(r2.status_code, 200)
        self.assertEqual(r2.json()["count"], 2)
        self.assertIn("fee_model", r2.json())
        self.assertEqual(
            len(r2.json()["markout"]["horizons"]),
            len(DEFAULT_MARKOUT_HORIZONS_SECONDS),
        )

    def test_net_equals_gross_minus_fee_with_override(self):
        os.environ["KEEL_SHADOW_TAKER_FEE_BPS"] = "7.5"
        os.environ["KEEL_SHADOW_FEE_ROLE"] = "taker"
        refresh_settings()
        self._seed_fill(ts=self.t0, action="BUY_LONG", price=100.0, probe=True)
        self._seed_factor(ts=self.t0 + 70, price=101.0)
        raw = compute_shadow_markout(
            self.ledger._get_conn(),
            hours=24.0,
            horizons=(60,),
            apply_funding=False,
        )
        self.assertEqual(raw["fee_model"]["source"], "override")
        h = raw["markout"]["horizons"][0]
        self.assertAlmostEqual(h["avg_markout_bps"], 100.0, places=4)
        self.assertAlmostEqual(h["avg_net_open_markout_bps"], 92.5, places=4)
        self.assertAlmostEqual(h["avg_net_roundtrip_markout_bps"], 85.0, places=4)

    def test_rebate_negative_maker_increases_net(self):
        os.environ["KEEL_SHADOW_MAKER_FEE_BPS"] = "-1.0"
        os.environ["KEEL_SHADOW_FEE_ROLE"] = "maker"
        refresh_settings()
        self._seed_fill(ts=self.t0, action="BUY_LONG", price=100.0)
        self._seed_factor(ts=self.t0 + 70, price=101.0)
        raw = compute_shadow_markout(
            self.ledger._get_conn(),
            hours=24.0,
            horizons=(60,),
            apply_funding=False,
        )
        h = raw["markout"]["horizons"][0]
        # net = gross - (-1) = gross + 1; RT = gross - (-2) = gross + 2
        self.assertAlmostEqual(h["avg_markout_bps"], 100.0, places=4)
        self.assertAlmostEqual(h["avg_net_open_markout_bps"], 101.0, places=4)
        self.assertAlmostEqual(h["avg_net_roundtrip_markout_bps"], 102.0, places=4)

    def test_skip_when_no_later_price(self):
        self._seed_fill(ts=self.t0, action="BUY_LONG", price=100.0)
        raw = self.ledger.get_shadow_markout(hours=24)
        for h in raw["markout"]["horizons"]:
            self.assertEqual(h["sample_count"], 0)
            self.assertEqual(h["skipped"], 1)

    def test_direct_helper_matches_ledger(self):
        self._seed_fill(ts=self.t0, action="BUY_LONG", price=50.0, probe=True)
        self._seed_factor(ts=self.t0 + 70, price=50.5)
        a = compute_shadow_markout(
            self.ledger._get_conn(), hours=24.0, horizons=(60,), apply_funding=False
        )
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
        self.assertIn("fee_model", a)
        self.assertIn("fee_model", b)


    def test_by_instrument_counts_and_markout_300s(self):
        # BTC long fill → profitable at 300s; ETH short fill → profitable at 300s
        self._seed_fill(
            ts=self.t0, action="BUY_LONG", price=100.0, probe=True, inst_id="BTC-USDT-SWAP"
        )
        self._seed_factor(ts=self.t0 + 310, price=101.0, inst_id="BTC-USDT-SWAP")
        self._seed_fill(
            ts=self.t0 + 20,
            action="SELL_SHORT",
            price=200.0,
            probe=False,
            inst_id="ETH-USDT-SWAP",
        )
        self._seed_factor(ts=self.t0 + 330, price=198.0, inst_id="ETH-USDT-SWAP")
        self.ledger.record_event(
            "shadow_near_probe_skip",
            inst_id="SOL-USDT-SWAP",
            data={"reason": "below_hurdle"},
            timestamp=self.t0 + 50,
        )

        body = self.client.get("/api/v1/stats/shadow?hours=24").json()
        by_inst = body.get("by_instrument") or {}
        self.assertIn("BTC-USDT-SWAP", by_inst)
        self.assertIn("ETH-USDT-SWAP", by_inst)
        self.assertIn("SOL-USDT-SWAP", by_inst)
        self.assertEqual(by_inst["BTC-USDT-SWAP"]["count"], 1)
        self.assertEqual(by_inst["BTC-USDT-SWAP"]["probe_count"], 1)
        self.assertEqual(by_inst["ETH-USDT-SWAP"]["count"], 1)
        self.assertEqual(by_inst["ETH-USDT-SWAP"]["probe_count"], 0)
        self.assertEqual(
            by_inst["SOL-USDT-SWAP"]["by_skip_reason"].get("below_hurdle"), 1
        )
        btc_mk = by_inst["BTC-USDT-SWAP"].get("markout_300s") or {}
        self.assertGreaterEqual(int(btc_mk.get("sample_count") or 0), 1)
        self.assertIsNotNone(btc_mk.get("avg_net_roundtrip_markout_bps"))
        eth_mk = by_inst["ETH-USDT-SWAP"].get("markout_300s") or {}
        self.assertGreaterEqual(int(eth_mk.get("sample_count") or 0), 1)


if __name__ == "__main__":
    unittest.main()
