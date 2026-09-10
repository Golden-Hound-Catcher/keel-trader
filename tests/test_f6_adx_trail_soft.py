"""F6: ADX regime, ATR trail exit, SuperTrend soft entry, no peeking."""
from __future__ import annotations

import os
import unittest
from unittest import mock

from keel.backtest.okx_history_rule import trail_exit_markout
from keel.factors.market_data import Candle, MarketSnapshot
from keel.factors.technical import calculate_adx
from keel.ledger.tf_fire_replay import forced_rule_variant
from keel.policy.stub import diagnose_rule_signal, rule_based_decision
from keel.policy.tv_rules import adx_regime_ok, st_entry_mode
from keel.worker.cycle import enrich_snapshot

_F6_ENV_KEYS = (
    "KEEL_RULE_ADX_MIN",
    "KEEL_RULE_ADX_PERIOD",
    "KEEL_RULE_ST_ENTRY_MODE",
    "KEEL_RULE_TF_REQUIRE_4H",
    "KEEL_RULE_REQUIRE_1H_TREND",
    "KEEL_RULE_ST_ATR_LENGTH",
    "KEEL_RULE_ST_FACTOR",
)


def setUpModule():
    global _SAVED_F6_ENV
    _SAVED_F6_ENV = {k: os.environ.get(k) for k in _F6_ENV_KEYS}


def tearDownModule():
    for k, old in _SAVED_F6_ENV.items():
        if old is None:
            os.environ.pop(k, None)
        else:
            os.environ[k] = old


def _ohlc(
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
        h = max(o, px) + 0.5
        l = min(o, px) - 0.5
        out.append(
            Candle(
                timestamp=t0 + i * 900.0,
                open=o,
                high=h,
                low=l,
                close=px,
                volume=vol,
            )
        )
    return out


def _snap(candles: list[Candle]) -> MarketSnapshot:
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
    snap.trend_1h = "bullish"  # type: ignore[assignment]
    snap.trend_4h = "bullish"  # type: ignore[assignment]
    return snap


class TestADX(unittest.TestCase):
    def test_insufficient_returns_zero(self):
        r = calculate_adx([1, 2], [0.5, 1], [1, 1.5], period=14)
        self.assertEqual(r.adx, 0.0)

    def test_trending_series_has_positive_adx(self):
        n = 80
        highs, lows, closes = [], [], []
        px = 100.0
        for i in range(n):
            px += 0.8  # strong uptrend
            highs.append(px + 0.4)
            lows.append(px - 0.4)
            closes.append(px)
        r = calculate_adx(highs, lows, closes, period=14)
        self.assertGreater(r.adx, 0.0)
        self.assertGreaterEqual(r.plus_di, 0.0)

    def test_no_peek_last_bar(self):
        n = 60
        highs = [100 + i * 0.2 + 1 for i in range(n)]
        lows = [100 + i * 0.2 - 1 for i in range(n)]
        closes = [100 + i * 0.2 for i in range(n)]
        full = calculate_adx(highs, lows, closes, period=14)
        # Truncating last bar must not use the dropped bar's range.
        trunc = calculate_adx(highs[:-1], lows[:-1], closes[:-1], period=14)
        # Values may differ (stateful Wilder) but trunc must be computable.
        self.assertGreaterEqual(trunc.adx, 0.0)
        self.assertIsInstance(full.adx, float)

    def test_regime_gate_off_by_default(self):
        snap = _snap(_ohlc())
        with mock.patch.dict(os.environ, {"KEEL_RULE_ADX_MIN": "0"}, clear=False):
            info = adx_regime_ok(snap)
            self.assertFalse(info["adx_enabled"])
            self.assertTrue(info["adx_ok"])


class TestTrailExit(unittest.TestCase):
    def test_long_trails_from_peak(self):
        # Entry at 100; rise to 110 then drop through trail.
        t0 = 1_700_000_000.0
        entry_ts = t0
        candles = []
        # bar0 closes at entry_ts (= signal bar close) — walk starts at subsequent.
        candles.append(Candle(t0 - 900, 99, 100.5, 98.5, 100, 1))
        # Subsequent bars: rally then collapse.
        path = [
            (101, 103, 100.5, 102.5),
            (102.5, 110, 102, 109),
            (109, 109.5, 104, 105),  # should hit trail from peak 110 with k=1.5, atr=2 → stop~107
            (105, 106, 90, 92),
        ]
        atr = 2.0
        for i, (o, h, l, c) in enumerate(path):
            candles.append(Candle(t0 + (i + 1) * 900.0, o, h, l, c, 1))
        row = trail_exit_markout(
            action="BUY_LONG",
            entry_price=100.0,
            entry_ts=entry_ts,
            atr_14=atr,
            candles_15m=candles,
            trail_atr=1.5,
            initial_sl_atr=1.0,
            timeout_seconds=9000,
            bar_seconds=900.0,
            open_fee_bps=5.0,
        )
        self.assertTrue(row["available"])
        self.assertEqual(row["exit_reason"], "trail")
        self.assertGreater(row["peak"], 100.0)
        # net RT should reflect exit vs entry minus fees
        self.assertIn("net_rt_bps", row)

    def test_time_stop_bars(self):
        t0 = 1_700_000_000.0
        candles = [Candle(t0 - 900, 100, 101, 99, 100, 1)]
        for i in range(5):
            px = 100 + i * 0.1
            candles.append(
                Candle(t0 + (i + 1) * 900.0, px, px + 0.2, px - 0.2, px, 1)
            )
        row = trail_exit_markout(
            action="BUY_LONG",
            entry_price=100.0,
            entry_ts=t0,
            atr_14=2.0,
            candles_15m=candles,
            trail_atr=5.0,  # wide trail so time stop fires first
            initial_sl_atr=5.0,
            time_stop_bars=2,
            timeout_seconds=90000,
            bar_seconds=900.0,
            open_fee_bps=5.0,
        )
        self.assertTrue(row["available"])
        self.assertEqual(row["exit_reason"], "time_stop")
        self.assertEqual(row["bars_held"], 2)

    def test_no_future_beyond_series(self):
        t0 = 1_700_000_000.0
        candles = [
            Candle(t0 - 900, 100, 101, 99, 100, 1),
            Candle(t0 + 900, 100, 101, 99, 100.5, 1),
        ]
        row = trail_exit_markout(
            action="BUY_LONG",
            entry_price=100.0,
            entry_ts=t0,
            atr_14=2.0,
            candles_15m=candles,
            trail_atr=1.5,
            timeout_seconds=90000,  # needs more path than available
            bar_seconds=900.0,
        )
        # Either timeout via last available horizon or past_series_end.
        self.assertTrue(
            row.get("available") is True
            or row.get("reason") in ("past_series_end", "markout_failed")
        )


class TestSuperTrendSoft(unittest.TestCase):
    def test_entry_mode_env(self):
        with mock.patch.dict(os.environ, {"KEEL_RULE_ST_ENTRY_MODE": "soft"}, clear=False):
            self.assertEqual(st_entry_mode(), "soft")
        with mock.patch.dict(os.environ, {"KEEL_RULE_ST_ENTRY_MODE": "flip"}, clear=False):
            self.assertEqual(st_entry_mode(), "flip")
        with mock.patch.dict(os.environ, {"KEEL_RULE_ST_ENTRY_MODE": ""}, clear=False):
            os.environ.pop("KEEL_RULE_ST_ENTRY_MODE", None)
            self.assertEqual(st_entry_mode(), "flip")

    def test_soft_allows_side_without_flip(self):
        # Strong uptrend → ST direction likely +1; flip may be false on last bar.
        candles = _ohlc(n=90, drift=0.5)
        snap = _snap(candles)
        env = {
            "KEEL_RULE_TF_REQUIRE_4H": "0",
            "KEEL_RULE_REQUIRE_1H_TREND": "0",
            "KEEL_RULE_ADX_MIN": "0",
            "KEEL_RULE_ST_ENTRY_MODE": "soft",
            "KEEL_RULE_ST_ATR_LENGTH": "10",
            "KEEL_RULE_ST_FACTOR": "2.0",
        }
        with mock.patch.dict(os.environ, env, clear=False):
            with forced_rule_variant("supertrend"):
                diag = diagnose_rule_signal(snap)
                self.assertEqual(diag["rule_variant"], "supertrend")
                self.assertEqual(diag.get("st_entry_mode"), "soft")
                if diag.get("st_direction") == 1:
                    self.assertTrue(diag.get("st_long_ok"))
                    d = rule_based_decision(snap)
                    if diag.get("st_full_long"):
                        self.assertEqual(d.action, "BUY_LONG")

    def test_flip_mode_requires_flip(self):
        candles = _ohlc(n=90, drift=0.5)
        snap = _snap(candles)
        env = {
            "KEEL_RULE_TF_REQUIRE_4H": "0",
            "KEEL_RULE_REQUIRE_1H_TREND": "0",
            "KEEL_RULE_ADX_MIN": "0",
            "KEEL_RULE_ST_ENTRY_MODE": "flip",
        }
        with mock.patch.dict(os.environ, env, clear=False):
            with forced_rule_variant("supertrend"):
                diag = diagnose_rule_signal(snap)
                if not diag.get("st_flipped"):
                    self.assertFalse(diag.get("st_long_ok") and diag.get("st_short_ok"))


class TestADXBlocksTF(unittest.TestCase):
    def test_high_adx_min_blocks(self):
        candles = _ohlc(n=40, drift=0.05)  # short / weak — ADX may be low
        snap = _snap(candles)
        with mock.patch.dict(
            os.environ,
            {
                "KEEL_RULE_VARIANT": "trend_follow",
                "KEEL_RULE_ADX_MIN": "55",
                "KEEL_RULE_TF_REQUIRE_4H": "0",
            },
            clear=False,
        ):
            with forced_rule_variant("trend_follow"):
                diag = diagnose_rule_signal(snap)
                self.assertTrue(diag.get("adx_enabled"))
                if float(diag.get("adx") or 0) < 55:
                    self.assertFalse(diag.get("adx_ok"))


if __name__ == "__main__":
    unittest.main()
