"""F0b: offline OKX history rule backtest (synthetic, no network)."""
from __future__ import annotations

import os
import unittest
from unittest.mock import patch

from keel.backtest.okx_history_rule import (
    barrier_exit_markout,
    excursion_markout,
    scale_exit_markout,
    trailing_supertrend_exit_markout,
    build_snapshot_at,
    fee_aware_markouts,
    forced_e31_rule_env,
    price_at_horizon,
    reprice_exit_rows,
    rows_to_series,
    walk_forward_backtest,
)
from keel.domain.decision import Decision
from keel.factors.market_data import Candle, MarketSnapshot
from keel.policy.stub import diagnose_rule_signal


def _row(ts_ms: float, px: float, vol: float = 1000.0) -> list[float]:
    return [float(ts_ms), px, px * 1.001, px * 0.999, px, vol]


def _rising_rows(
    n: int,
    *,
    start_ms: float,
    step_ms: float,
    start_px: float = 100.0,
    drift: float = 0.002,
    vol: float = 2000.0,
) -> list[list[float]]:
    rows: list[list[float]] = []
    px = start_px
    for i in range(n):
        rows.append(_row(start_ms + i * step_ms, px, vol=vol))
        px *= 1.0 + drift
    return rows


class TestPriceHorizon(unittest.TestCase):
    def test_interp_between_closes(self):
        # Two 15m bars: opens at 0 and 900; closes at 900 and 1800.
        candles = [
            Candle(0.0, 100, 101, 99, 100.0, 1.0),
            Candle(900.0, 100, 102, 99, 110.0, 1.0),
        ]
        # entry at first close (900); +450s → halfway to 110
        found = price_at_horizon(candles, entry_ts=900.0, horizon_seconds=450.0)
        self.assertIsNotNone(found)
        assert found is not None
        _ts, px, src = found
        self.assertEqual(src, "interp_close")
        self.assertAlmostEqual(px, 105.0, places=5)


class TestFeeMarkout(unittest.TestCase):
    def test_long_net_rt_subtracts_10bps(self):
        candles = [
            Candle(0.0, 100, 101, 99, 100.0, 1.0),
            Candle(900.0, 100, 102, 99, 100.2, 1.0),  # +20 bps gross over 900s
        ]
        mos = fee_aware_markouts(
            action="BUY_LONG",
            entry_price=100.0,
            entry_ts=900.0,
            candles_15m=candles,
            horizons=(900,),
            open_fee_bps=5.0,
            clear_hurdle_bps=10.0,
        )
        row = mos[900]
        self.assertTrue(row["available"])
        self.assertAlmostEqual(row["gross_bps"], 20.0, places=4)
        self.assertAlmostEqual(row["net_rt_bps"], 10.0, places=4)
        self.assertTrue(row["clears_hurdle"])


class TestRepriceExit(unittest.TestCase):
    def test_maker_rt_is_six_bps_better_than_taker_on_same_gross(self):
        rows = [
            {
                "available": True,
                "gross_bps": 0.0,
                "exit_reason": "timeout",
                "hold_seconds": 14400.0,
            },
            {
                "available": True,
                "gross_bps": 8.0,
                "exit_reason": "tp",
                "hold_seconds": 1800.0,
            },
        ]
        taker = reprice_exit_rows(rows, open_fee_bps=5.0, clear_hurdle_bps=10.0)
        maker = reprice_exit_rows(rows, open_fee_bps=2.0, clear_hurdle_bps=10.0)
        self.assertEqual(taker["n_available"], 2)
        self.assertEqual(maker["fee_role"], "maker")
        self.assertAlmostEqual(taker["round_trip_fee_bps"], 10.0, places=6)
        self.assertAlmostEqual(maker["round_trip_fee_bps"], 4.0, places=6)
        assert taker["avg_net_rt_bps"] is not None
        assert maker["avg_net_rt_bps"] is not None
        self.assertAlmostEqual(
            maker["avg_net_rt_bps"] - taker["avg_net_rt_bps"], 6.0, places=6
        )
        self.assertEqual(maker["by_exit_reason"], {"timeout": 1, "tp": 1})


class TestRequire4hPath(unittest.TestCase):
    def tearDown(self) -> None:
        for k in (
            "KEEL_RULE_VARIANT",
            "KEEL_RULE_TF_REQUIRE_4H",
            "KEEL_RULE_TF_MACD_LAG_BPS",
            "KEEL_RULE_TF_MAX_EXTENSION_ATR",
            "KEEL_RULE_TF_PULLBACK",
        ):
            os.environ.pop(k, None)

    def _tf_long_snap(self, trend_4h: str = "bullish") -> MarketSnapshot:
        return MarketSnapshot(
            inst_id="BTC-USDT-SWAP",
            name="BTC",
            timestamp=1_700_000_000.0,
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
            trend_4h=trend_4h,  # type: ignore[arg-type]
            data_valid=True,
            data_quality_reason="test",
        )

    def test_require_4h_blocks_neutral_4h(self):
        with forced_e31_rule_env(variant="trend_follow", require_4h=True):
            diag = diagnose_rule_signal(self._tf_long_snap(trend_4h="neutral"))
        self.assertTrue(diag.get("require_4h_trend"))
        self.assertIn("trend_bullish", diag.get("missing") or [])

    def test_require_4h_passes_aligned(self):
        with forced_e31_rule_env(variant="trend_follow", require_4h=True):
            diag = diagnose_rule_signal(self._tf_long_snap(trend_4h="bullish"))
        self.assertTrue(diag.get("require_4h_trend"))
        self.assertEqual(diag.get("missing"), [])
        self.assertIn("4h", str(diag.get("trend_gate") or ""))


class TestCooldownWalk(unittest.TestCase):
    def tearDown(self) -> None:
        for k in (
            "KEEL_RULE_VARIANT",
            "KEEL_RULE_TF_REQUIRE_4H",
            "KEEL_RULE_TF_MACD_LAG_BPS",
            "KEEL_RULE_TF_MAX_EXTENSION_ATR",
            "KEEL_RULE_TF_PULLBACK",
        ):
            os.environ.pop(k, None)

    def test_synthetic_series_cooldown_and_4h_snapshot(self):
        # Tiny rising multi-TF history (no network).
        start = 1_700_000_000_000.0  # ms
        # Longer higher-TF history so index 70 has ≥5 fully closed 1h/4h bars.
        rows_15 = _rising_rows(80, start_ms=start, step_ms=900_000.0)
        rows_1h = _rising_rows(120, start_ms=start - 100 * 3_600_000.0, step_ms=3_600_000.0)
        rows_4h = _rising_rows(120, start_ms=start - 100 * 14_400_000.0, step_ms=14_400_000.0)
        series = rows_to_series("BTC-USDT-SWAP", rows_15, rows_1h, rows_4h)

        # Snapshot at last closed bar must not include future 4h opens.
        snap = build_snapshot_at(series, index_15m=70, lookback=64)
        decision_ts = float(snap.timestamp)
        for c in snap.candles_4h:
            self.assertLessEqual(float(c.timestamp) + 14_400.0, decision_ts + 1e-6)

        full_diag = {
            "missing": [],
            "nearest": "long",
            "require_4h_trend": True,
            "trend_gate": "15m+1h+4h",
            "rule_variant": "trend_follow",
        }

        def _always_fire(snapshot: MarketSnapshot) -> Decision:
            px = float(snapshot.price or 100.0)
            return Decision(
                inst_id=snapshot.inst_id,
                action="BUY_LONG",
                confidence=80.0,
                reason="test fire",
                entry_price=px,
                take_profit=px * 1.03,
                stop_loss=px * 0.99,
                signal_diag=dict(full_diag),
            )

        def _always_diag(snapshot: MarketSnapshot) -> dict:
            return dict(full_diag)

        with patch(
            "keel.backtest.okx_history_rule.rule_based_decision",
            side_effect=_always_fire,
        ), patch(
            "keel.backtest.okx_history_rule.diagnose_rule_signal",
            side_effect=_always_diag,
        ):
            summary = walk_forward_backtest(
                [series],
                variant="trend_follow",
                cooldown_seconds=900,
                lookback=64,
                horizons=(60, 300, 900),
                require_4h=True,
            )

        # Steps every bar after warmup; fires suppressed on adjacent 900s bars.
        self.assertGreater(summary["n_steps"], 10)
        self.assertGreater(summary["full_gate_count"], 0)
        self.assertGreater(summary["cooldown_suppressed"], 0)
        # Cooldown must suppress some adjacent fires (900s bars + 900s cd).
        self.assertLess(summary["full_gate_count"], summary["n_steps"])
        self.assertGreater(
            summary["cooldown_suppressed"] + summary["full_gate_count"],
            summary["full_gate_count"],
        )
        # 4h path forced on in env
        self.assertTrue(summary["require_4h"])


class TestScriptSmoke(unittest.TestCase):
    def test_script_help(self):
        import subprocess
        import sys
        from pathlib import Path

        root = Path(__file__).resolve().parents[1]
        env = dict(os.environ)
        env["KEEL_SKIP_DOTENV"] = "1"
        for k in (
            "KEEL_OKX_API_KEY",
            "KEEL_OKX_SECRET_KEY",
            "KEEL_OKX_PASSPHRASE",
            "OKX_API_KEY",
            "OKX_SECRET_KEY",
            "OKX_PASSPHRASE",
        ):
            env[k] = ""
        env["PYTHONPATH"] = str(root)
        proc = subprocess.run(
            [sys.executable, str(root / "scripts" / "okx_history_rule_backtest.py"), "--help"],
            capture_output=True,
            text=True,
            env=env,
            check=False,
        )
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertIn("bars-15m", proc.stdout)

    def test_compare_script_help(self):
        import subprocess
        import sys
        from pathlib import Path

        root = Path(__file__).resolve().parents[1]
        env = dict(os.environ)
        env["KEEL_SKIP_DOTENV"] = "1"
        for k in (
            "KEEL_OKX_API_KEY",
            "KEEL_OKX_SECRET_KEY",
            "KEEL_OKX_PASSPHRASE",
            "OKX_API_KEY",
            "OKX_SECRET_KEY",
            "OKX_PASSPHRASE",
        ):
            env[k] = ""
        env["PYTHONPATH"] = str(root)
        proc = subprocess.run(
            [sys.executable, str(root / "scripts" / "okx_history_strategy_compare.py"), "--help"],
            capture_output=True,
            text=True,
            env=env,
            check=False,
        )
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertIn("cooldown-seconds", proc.stdout)
        self.assertIn("skip-trail", proc.stdout)
        self.assertIn("skip-regime", proc.stdout)
        self.assertIn("skip-score", proc.stdout)
        self.assertIn("skip-mfe", proc.stdout)
        self.assertIn("skip-early-tp", proc.stdout)


class TestBarrierExitMarkout(unittest.TestCase):
    """F2c: ATR barrier TP/SL/timeout on subsequent 15m OHLC path."""

    def test_long_hits_tp(self):
        # entry at close of bar0 (ts=900); next bar high clears TP.
        atr = 1.0
        entry = 100.0
        tp = entry + 2.2 * atr  # 102.2
        candles = [
            Candle(0.0, 100, 100.5, 99.5, 100.0, 1.0),
            Candle(900.0, 100, 103.0, 99.8, 102.5, 1.0),  # high hits TP
        ]
        row = barrier_exit_markout(
            action="BUY_LONG",
            entry_price=entry,
            entry_ts=900.0,
            atr_14=atr,
            candles_15m=candles,
            open_fee_bps=5.0,
            clear_hurdle_bps=10.0,
        )
        self.assertTrue(row["available"])
        self.assertEqual(row["exit_reason"], "tp")
        self.assertAlmostEqual(row["exit_price"], tp, places=5)
        # gross = (102.2-100)/100 * 1e4 = 220 bps; net_rt = 220 - 10 = 210
        self.assertAlmostEqual(row["gross_bps"], 220.0, places=4)
        self.assertAlmostEqual(row["net_rt_bps"], 210.0, places=4)
        self.assertTrue(row["clears_hurdle"])

    def test_long_hits_sl_before_tp_same_bar(self):
        atr = 1.0
        entry = 100.0
        sl = entry - 1.0 * atr  # 99.0
        candles = [
            Candle(0.0, 100, 100.5, 99.5, 100.0, 1.0),
            Candle(900.0, 100, 103.0, 98.5, 101.0, 1.0),  # both SL+TP → SL first
        ]
        row = barrier_exit_markout(
            action="BUY_LONG",
            entry_price=entry,
            entry_ts=900.0,
            atr_14=atr,
            candles_15m=candles,
            open_fee_bps=5.0,
        )
        self.assertTrue(row["available"])
        self.assertEqual(row["exit_reason"], "sl")
        self.assertAlmostEqual(row["exit_price"], sl, places=5)

    def test_timeout_uses_horizon_price(self):
        atr = 1.0
        entry = 100.0
        # Next bars never hit TP/SL; timeout at 900s → close of next bar path.
        candles = [
            Candle(0.0, 100, 100.2, 99.8, 100.0, 1.0),
            Candle(900.0, 100, 100.4, 99.7, 100.1, 1.0),  # +10 bps at close
        ]
        row = barrier_exit_markout(
            action="BUY_LONG",
            entry_price=entry,
            entry_ts=900.0,
            atr_14=atr,
            candles_15m=candles,
            timeout_seconds=900,
            open_fee_bps=5.0,
        )
        self.assertTrue(row["available"])
        self.assertEqual(row["exit_reason"], "timeout")
        self.assertAlmostEqual(row["gross_bps"], 10.0, places=4)
        self.assertAlmostEqual(row["net_rt_bps"], 0.0, places=4)

    def test_short_hits_tp(self):
        atr = 2.0
        entry = 200.0
        tp = entry - 2.2 * atr  # 195.6
        candles = [
            Candle(0.0, 200, 201, 199, 200.0, 1.0),
            Candle(900.0, 200, 200.5, 195.0, 196.0, 1.0),
        ]
        row = barrier_exit_markout(
            action="SELL_SHORT",
            entry_price=entry,
            entry_ts=900.0,
            atr_14=atr,
            candles_15m=candles,
            open_fee_bps=5.0,
        )
        self.assertTrue(row["available"])
        self.assertEqual(row["exit_reason"], "tp")
        self.assertAlmostEqual(row["exit_price"], tp, places=5)


class TestExcursionAndEarlyTp(unittest.TestCase):
    """MFE/MAE path + 1R early TP / scale-out (measurement only)."""

    def test_long_mfe_touches_1r_then_sl(self):
        atr = 1.0
        entry = 100.0
        candles = [
            Candle(0.0, 100, 100.2, 99.8, 100.0, 1.0),
            # subsequent: high +1.2R then later bar dumps to SL
            Candle(900.0, 100.0, 101.2, 99.6, 101.0, 1.0),
            Candle(1800.0, 101.0, 101.1, 98.9, 99.0, 1.0),
        ]
        row = excursion_markout(
            action="BUY_LONG",
            entry_price=entry,
            entry_ts=900.0,
            atr_14=atr,
            candles_15m=candles,
            timeout_seconds=14_400,
        )
        self.assertTrue(row["available"])
        self.assertGreaterEqual(row["mfe_r"], 1.0)
        self.assertTrue(row["touch"]["1"])
        self.assertEqual(row["after_1r"], "sl")
        self.assertEqual(row["path_end"], "sl")

    def test_long_mfe_15m_does_not_see_later_1r(self):
        atr = 1.0
        entry = 100.0
        candles = [
            Candle(0.0, 100, 100.2, 99.8, 100.0, 1.0),
            Candle(900.0, 100.0, 100.4, 99.7, 100.2, 1.0),  # 15m: 0.4R
            Candle(1800.0, 100.2, 101.2, 100.0, 101.0, 1.0),  # later 1.2R
        ]
        row = excursion_markout(
            action="BUY_LONG",
            entry_price=entry,
            entry_ts=900.0,
            atr_14=atr,
            timeout_seconds=14_400,
            snapshot_seconds=900,
            candles_15m=candles,
        )
        self.assertTrue(row["available"])
        self.assertFalse(row["touch_15m"]["1"])
        self.assertTrue(row["touch"]["1"])

    def test_early_tp_1r_hits(self):
        atr = 1.0
        entry = 100.0
        tp = entry + 1.0 * atr
        candles = [
            Candle(0.0, 100, 100.2, 99.8, 100.0, 1.0),
            Candle(900.0, 100, 101.5, 99.8, 101.2, 1.0),
        ]
        row = barrier_exit_markout(
            action="BUY_LONG",
            entry_price=entry,
            entry_ts=900.0,
            atr_14=atr,
            candles_15m=candles,
            tp_atr=1.0,
            open_fee_bps=5.0,
        )
        self.assertEqual(row["exit_reason"], "tp")
        self.assertAlmostEqual(row["exit_price"], tp, places=5)
        # 100 bps gross - 10 RT = 90
        self.assertAlmostEqual(row["gross_bps"], 100.0, places=4)
        self.assertAlmostEqual(row["net_rt_bps"], 90.0, places=4)

    def test_scale_half_then_distant_tp(self):
        atr = 1.0
        entry = 100.0
        candles = [
            Candle(0.0, 100, 100.2, 99.8, 100.0, 1.0),
            Candle(900.0, 100, 101.2, 99.8, 101.1, 1.0),  # hits 1R
            Candle(1800.0, 101.1, 102.5, 101.0, 102.3, 1.0),  # hits 2.2
        ]
        row = scale_exit_markout(
            action="BUY_LONG",
            entry_price=entry,
            entry_ts=900.0,
            atr_14=atr,
            candles_15m=candles,
            timeout_seconds=2700,
            open_fee_bps=5.0,
        )
        self.assertTrue(row["available"])
        self.assertTrue(row["scaled"])
        self.assertEqual(row["exit_reason"], "scale_then_tp")
        # 0.5*100 + 0.5*220 = 160 gross; net 150
        self.assertAlmostEqual(row["gross_bps"], 160.0, places=3)
        self.assertAlmostEqual(row["net_rt_bps"], 150.0, places=3)

    def test_scale_sl_before_1r_not_scaled(self):
        atr = 1.0
        entry = 100.0
        candles = [
            Candle(0.0, 100, 100.2, 99.8, 100.0, 1.0),
            Candle(900.0, 100, 100.4, 98.5, 99.0, 1.0),
        ]
        row = scale_exit_markout(
            action="BUY_LONG",
            entry_price=entry,
            entry_ts=900.0,
            atr_14=atr,
            candles_15m=candles,
            open_fee_bps=5.0,
        )
        self.assertTrue(row["available"])
        self.assertFalse(row["scaled"])
        self.assertEqual(row["exit_reason"], "sl")

    def tearDown(self) -> None:
        for k in (
            "KEEL_RULE_VARIANT",
            "KEEL_RULE_TF_REQUIRE_4H",
            "KEEL_RULE_TF_MACD_LAG_BPS",
            "KEEL_RULE_TF_MAX_EXTENSION_ATR",
            "KEEL_RULE_TF_PULLBACK",
        ):
            os.environ.pop(k, None)

    def test_include_barrier_key_present(self):
        # Minimal synthetic rising series — may or may not fire; just ensure
        # summarize exposes barrier=None/dict without crashing when flag on.
        start = 1_700_000_000_000.0
        rows_15 = _rising_rows(80, start_ms=start, step_ms=900_000.0)
        rows_1h = _rising_rows(40, start_ms=start, step_ms=3_600_000.0)
        rows_4h = _rising_rows(30, start_ms=start, step_ms=14_400_000.0)
        series = rows_to_series("BTC-USDT-SWAP", rows_15, rows_1h, rows_4h)
        with patch(
            "keel.backtest.okx_history_rule.rule_based_decision",
            return_value=Decision(
                inst_id="BTC-USDT-SWAP",
                action="WAIT",
                confidence=0.0,
                reason="test",
                entry_price=0.0,
                take_profit=0.0,
                stop_loss=0.0,
                signal_diag={"missing": ["rsi_ok"]},
            ),
        ):
            summary = walk_forward_backtest(
                [series],
                variant="trend_follow",
                cooldown_seconds=900,
                include_barrier=True,
            )
        self.assertIn("barrier", summary["markout"])
        # No fires → barrier summary still built with n_available=0
        b = summary["markout"]["barrier"]
        self.assertIsNotNone(b)
        self.assertEqual(b["n_available"], 0)


class TestTrailingSupertrendExit(unittest.TestCase):
    """P0: Supertrend trail SL / flip / timeout on subsequent 15m OHLC."""

    def _flat(self, n: int, *, px: float = 100.0, start: float = 0.0) -> list[Candle]:
        out: list[Candle] = []
        for i in range(n):
            out.append(
                Candle(start + i * 900.0, px, px + 0.5, px - 0.5, px, 1.0)
            )
        return out

    def test_long_hits_initial_atr_sl(self):
        atr = 1.0
        entry = 100.0
        sl = entry - 1.0 * atr  # 99
        candles = self._flat(20)
        # Subsequent bar dumps through SL.
        candles.append(Candle(20 * 900.0, 100.0, 100.2, 98.0, 99.0, 1.0))
        row = trailing_supertrend_exit_markout(
            action="BUY_LONG",
            entry_price=entry,
            entry_ts=20 * 900.0,  # close of last flat bar (index 19)
            atr_14=atr,
            candles_15m=candles,
            timeout_seconds=14_400,
            open_fee_bps=5.0,
            clear_hurdle_bps=10.0,
        )
        self.assertTrue(row["available"])
        self.assertEqual(row["exit_reason"], "sl")
        self.assertAlmostEqual(row["exit_price"], sl, places=5)
        self.assertLess(row["net_rt_bps"], 0.0)

    def test_timeout_when_path_never_hits(self):
        atr = 1.0
        entry = 100.0
        candles = self._flat(22)
        row = trailing_supertrend_exit_markout(
            action="BUY_LONG",
            entry_price=entry,
            entry_ts=20 * 900.0,
            atr_14=atr,
            candles_15m=candles,
            timeout_seconds=900,
            open_fee_bps=5.0,
        )
        self.assertTrue(row["available"])
        self.assertEqual(row["exit_reason"], "timeout")

    def test_include_trail_key_present(self):
        start = 1_700_000_000_000.0
        rows_15 = _rising_rows(80, start_ms=start, step_ms=900_000.0)
        rows_1h = _rising_rows(40, start_ms=start, step_ms=3_600_000.0)
        rows_4h = _rising_rows(30, start_ms=start, step_ms=14_400_000.0)
        series = rows_to_series("BTC-USDT-SWAP", rows_15, rows_1h, rows_4h)
        with patch(
            "keel.backtest.okx_history_rule.rule_based_decision",
            return_value=Decision(
                inst_id="BTC-USDT-SWAP",
                action="WAIT",
                confidence=0.0,
                reason="test",
                entry_price=0.0,
                take_profit=0.0,
                stop_loss=0.0,
                signal_diag={"missing": ["rsi_ok"]},
            ),
        ):
            summary = walk_forward_backtest(
                [series],
                variant="trend_follow",
                cooldown_seconds=900,
                include_trail=True,
            )
        self.assertIn("trail", summary["markout"])
        t = summary["markout"]["trail"]
        self.assertIsNotNone(t)
        self.assertEqual(t["n_available"], 0)


if __name__ == "__main__":
    unittest.main()
