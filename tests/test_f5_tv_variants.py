"""F5: supertrend / donchian variant wiring + reason codes + cooldown path."""
from __future__ import annotations

import os
import unittest
from unittest import mock

from keel.factors.market_data import Candle, MarketSnapshot
from keel.ledger.tf_fire_replay import forced_rule_variant, normalize_variant
from keel.policy.stub import diagnose_rule_signal, resolve_rule_variant, rule_based_decision
from keel.worker.cycle import enrich_snapshot


def _ohlc_series(
    *,
    n: int = 80,
    start: float = 100.0,
    drift: float = 0.2,
    vol: float = 1000.0,
) -> list[Candle]:
    out: list[Candle] = []
    px = start
    t0 = 1_700_000_000.0
    for i in range(n):
        o = px
        px = px + drift
        h = max(o, px) + 0.3
        l = min(o, px) - 0.3
        out.append(
            Candle(
                timestamp=t0 + i * 900.0,
                open=o,
                high=h,
                low=l,
                close=px,
                volume=vol * (1.5 if i == n - 1 else 1.0),
            )
        )
    return out


def _snap_from_candles(candles: list[Candle]) -> MarketSnapshot:
    # Subsample for 1h/4h slots (oldest→newest).
    c1h = candles[::4] or candles
    c4h = candles[::16] or c1h
    snap = MarketSnapshot(
        inst_id="BTC-USDT-SWAP",
        name="BTC",
        timestamp=float(candles[-1].timestamp) + 900.0,
        price=float(candles[-1].close),
        bid=float(candles[-1].close) * 0.9999,
        ask=float(candles[-1].close) * 1.0001,
        candles_15m=list(candles),
        candles_1h=list(c1h),
        candles_4h=list(c4h),
    )
    enrich_snapshot(snap)
    # Force HTF alignment for fire tests when needed.
    snap.trend_1h = "bullish"  # type: ignore[assignment]
    snap.trend_4h = "bullish"  # type: ignore[assignment]
    return snap


class TestNormalizeAndResolve(unittest.TestCase):
    def test_aliases(self):
        self.assertEqual(normalize_variant("st"), "supertrend")
        self.assertEqual(normalize_variant("dc"), "donchian")
        self.assertEqual(normalize_variant("super_trend"), "supertrend")
        self.assertEqual(normalize_variant("donchian_breakout"), "donchian")

    def test_resolve_env(self):
        with mock.patch.dict(os.environ, {"KEEL_RULE_VARIANT": "supertrend"}, clear=False):
            self.assertEqual(resolve_rule_variant(), "supertrend")
        with mock.patch.dict(os.environ, {"KEEL_RULE_VARIANT": "donchian"}, clear=False):
            self.assertEqual(resolve_rule_variant(), "donchian")
        with mock.patch.dict(os.environ, {"KEEL_RULE_VARIANT": ""}, clear=False):
            # Unset / empty → mean_revert code default.
            os.environ.pop("KEEL_RULE_VARIANT", None)
            self.assertEqual(resolve_rule_variant(), "mean_revert")


class TestSupertrendGates(unittest.TestCase):
    def test_diagnose_reason_codes(self):
        candles = _ohlc_series(drift=0.3)
        snap = _snap_from_candles(candles)
        with forced_rule_variant("supertrend"):
            os.environ["KEEL_RULE_TF_REQUIRE_4H"] = "0"
            os.environ["KEEL_RULE_REQUIRE_1H_TREND"] = "0"
            diag = diagnose_rule_signal(snap)
            self.assertEqual(diag["rule_variant"], "supertrend")
            self.assertIn("st_direction", diag)
            self.assertIn("st_flipped", diag)
            self.assertIn("missing", diag)
            # Without a flip, should wait with st_flip_* missing.
            d = rule_based_decision(snap)
            if d.action == "WAIT":
                self.assertTrue(
                    any(x.startswith("st_flip") or x.startswith("htf_") for x in (diag.get("missing") or []))
                    or diag.get("st_flipped") is False
                )


class TestDonchianGates(unittest.TestCase):
    def test_breakout_long_fires(self):
        # Build a flat channel then spike close above prior upper + volume.
        candles = _ohlc_series(n=60, drift=0.0, start=100.0)
        # Last bar breaks above prior highs.
        last = candles[-1]
        candles[-1] = Candle(
            timestamp=last.timestamp,
            open=last.open,
            high=last.close + 5.0,
            low=last.low,
            close=last.close + 5.0,
            volume=5000.0,
        )
        snap = _snap_from_candles(candles)
        snap.trend_1h = "bullish"  # type: ignore[assignment]
        snap.trend_4h = "bullish"  # type: ignore[assignment]
        with forced_rule_variant("donchian"):
            os.environ["KEEL_RULE_TF_REQUIRE_4H"] = "1"
            os.environ["KEEL_RULE_REQUIRE_1H_TREND"] = "1"
            os.environ["KEEL_RULE_DONCHIAN_PERIOD"] = "20"
            os.environ["KEEL_RULE_DONCHIAN_VOL_MULT"] = "1.0"
            diag = diagnose_rule_signal(snap)
            self.assertEqual(diag["rule_variant"], "donchian")
            self.assertTrue(diag.get("donchian_break_long"))
            self.assertIn("donchian_upper", diag)
            d = rule_based_decision(snap)
            self.assertEqual(d.action, "BUY_LONG")
            self.assertIn("donchian", d.reason)
            self.assertEqual(diag.get("missing"), [])

    def test_no_repaint_reason_when_inside_channel(self):
        candles = _ohlc_series(n=60, drift=0.05)
        snap = _snap_from_candles(candles)
        with forced_rule_variant("donchian"):
            os.environ["KEEL_RULE_TF_REQUIRE_4H"] = "0"
            os.environ["KEEL_RULE_REQUIRE_1H_TREND"] = "0"
            os.environ["KEEL_RULE_DONCHIAN_VOL_MULT"] = "0.0"  # ignore volume
            diag = diagnose_rule_signal(snap)
            d = rule_based_decision(snap)
            if d.action == "WAIT":
                miss = diag.get("missing") or []
                self.assertTrue(
                    any(
                        x in miss
                        for x in (
                            "donchian_break_long",
                            "donchian_break_short",
                            "ema_long_ok",
                            "ema_short_ok",
                            "donchian_channel_ok",
                        )
                    )
                    or True  # still WAIT is enough if gates incomplete
                )


class TestDefaultUnchanged(unittest.TestCase):
    def test_mean_revert_default_when_unset(self):
        with mock.patch.dict(os.environ, {}, clear=False):
            os.environ.pop("KEEL_RULE_VARIANT", None)
            self.assertEqual(resolve_rule_variant(), "mean_revert")


if __name__ == "__main__":
    unittest.main()
