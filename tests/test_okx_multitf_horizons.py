"""F4: multi-TF horizon mapping + no train/valid cross-contamination."""
from __future__ import annotations

import unittest

from keel.backtest.multitf import (
    ENTRY_BARS,
    assert_no_horizon_cross_mix,
    entry_tf_spec,
    horizons_for_entry_bar,
    modest_multitf_grid,
    primary_horizon_seconds,
    rows_to_entry_series,
    series_for_train_markout_multitf,
    windows_do_not_overlap,
)
from keel.backtest.okx_history_rule import (
    build_snapshot_at,
    price_at_horizon,
    walk_forward_backtest,
)
from keel.backtest.train_valid import (
    make_train_valid_windows,
    run_config_on_window,
    StrategyConfig,
)
from keel.factors.market_data import Candle
from unittest.mock import patch


class TestHorizonMapping(unittest.TestCase):
    def test_all_entry_bars_declared(self):
        self.assertEqual(list(ENTRY_BARS), ["5m", "15m", "30m", "1H", "4H"])
        for b in ENTRY_BARS:
            spec = entry_tf_spec(b)
            self.assertEqual(spec.bar, b)
            self.assertGreater(spec.primary_bars, 0)
            self.assertGreaterEqual(spec.primary_bars_max, spec.primary_bars_min)
            self.assertGreaterEqual(spec.primary_bars, spec.primary_bars_min)
            self.assertLessEqual(spec.primary_bars, spec.primary_bars_max)

    def test_horizons_are_bar_multiples_not_fixed_300(self):
        """15m/1H/4H primary must NOT be the old wall-clock 300s clear horizon."""
        for b in ("15m", "30m", "1H", "4H"):
            prim = primary_horizon_seconds(b)
            self.assertNotEqual(
                prim,
                300,
                msg=f"{b} primary must not be fixed 300s wall-clock",
            )
            spec = entry_tf_spec(b)
            self.assertEqual(prim % spec.bar_seconds, 0)
            self.assertEqual(prim // spec.bar_seconds, spec.primary_bars)

    def test_5m_primary_short_range(self):
        spec = entry_tf_spec("5m")
        self.assertEqual(spec.bar_seconds, 300)
        self.assertEqual(spec.primary_bars_min, 3)
        self.assertEqual(spec.primary_bars_max, 6)
        # 3–6 bars → 900–1800s; primary mid=4 → 1200s
        self.assertEqual(spec.primary_seconds, 1200)
        self.assertEqual(spec.secondary_seconds, 12 * 300)
        hs = horizons_for_entry_bar("5m")
        self.assertIn(900, hs)
        self.assertIn(1200, hs)
        self.assertIn(3600, hs)

    def test_4h_primary_long_range(self):
        spec = entry_tf_spec("4H")
        self.assertEqual(spec.bar_seconds, 14400)
        self.assertEqual(spec.primary_bars_min, 3)
        self.assertEqual(spec.primary_bars_max, 6)
        self.assertEqual(spec.primary_seconds, 4 * 14400)  # 16h
        # Distinct from 5m primary
        self.assertNotEqual(spec.primary_seconds, primary_horizon_seconds("5m"))

    def test_no_cross_mix_across_entry_bars(self):
        for i, a in enumerate(ENTRY_BARS):
            for b in ENTRY_BARS[i + 1 :]:
                assert_no_horizon_cross_mix(a, b)
                self.assertNotEqual(
                    primary_horizon_seconds(a),
                    primary_horizon_seconds(b),
                )

    def test_confirm_mapping_practical(self):
        self.assertEqual(entry_tf_spec("5m").confirm_mid, "15m")
        self.assertEqual(entry_tf_spec("5m").confirm_high, "1H")
        self.assertEqual(entry_tf_spec("15m").confirm_mid, "1H")
        self.assertEqual(entry_tf_spec("15m").confirm_high, "4H")
        self.assertEqual(entry_tf_spec("1H").confirm_high, "1D")
        self.assertEqual(entry_tf_spec("4H").confirm_high, "1D")


class TestNoCrossContamination(unittest.TestCase):
    def test_windows_adjacent_no_overlap(self):
        now = 1_700_000_000.0
        w = make_train_valid_windows(now)
        self.assertTrue(
            windows_do_not_overlap(
                train_start=w.train_start_ts,
                train_end=w.train_end_ts,
                valid_start=w.valid_start_ts,
                valid_end=w.valid_end_ts,
            )
        )
        # Overlapping windows must fail the check.
        self.assertFalse(
            windows_do_not_overlap(
                train_start=w.train_start_ts,
                train_end=w.valid_end_ts,
                valid_start=w.valid_start_ts,
                valid_end=w.valid_end_ts,
            )
        )

    def test_train_truncate_drops_valid_opens_any_entry_bar(self):
        train_end = 1_700_000_000.0
        for bar, step in (("5m", 300.0), ("1H", 3600.0), ("4H", 14400.0)):
            candles = [
                Candle(train_end - 2 * step, 1, 1, 1, 1, 1),
                Candle(train_end - step, 1, 1, 1, 1, 1),
                Candle(train_end, 1, 1, 1, 1, 1),
                Candle(train_end + step, 1, 1, 1, 1, 1),
            ]
            series = rows_to_entry_series(
                "BTC-USDT-SWAP",
                [[c.timestamp * 1000, 1, 1, 1, 1, 1] for c in candles],
                [[c.timestamp * 1000, 1, 1, 1, 1, 1] for c in candles],
                [[c.timestamp * 1000, 1, 1, 1, 1, 1] for c in candles],
                entry_bar=bar,
            )
            trunc = series_for_train_markout_multitf(
                series, train_end_ts=train_end
            )
            self.assertEqual(trunc.entry_bar, bar)
            self.assertTrue(
                all(c.timestamp < train_end for c in trunc.candles_15m),
                msg=f"{bar} train trunc leaked valid opens",
            )
            self.assertEqual(len(trunc.candles_15m), 2)

    def test_run_config_forwards_entry_bar_and_horizons(self):
        cfg = StrategyConfig(
            variant="trend_follow",
            require_4h=True,
            max_extension_atr=0.0,
            pullback=False,
            rsi_pullback_long_max=None,
            rsi_pullback_short_min=None,
            cooldown_seconds=3600,
        )
        captured: dict = {}

        def _fake_walk(*_a, **kwargs):
            captured.update(kwargs)
            return {
                "full_gate_count": 0,
                "full_gate_rate": 0.0,
                "n_steps": 0,
                "entry_bar": kwargs.get("entry_bar"),
                "markout": {},
            }

        from keel.backtest import train_valid as tv

        with patch.object(tv, "walk_forward_backtest", side_effect=_fake_walk):
            tv.run_config_on_window(
                [],
                cfg,
                decision_ts_min=100.0,
                decision_ts_max=200.0,
                horizons=(1200, 3600),
                clear_horizon_seconds=1200,
                entry_bar="5m",
                confirm_mid_bar="15m",
                confirm_high_bar="1H",
            )
        self.assertEqual(captured.get("entry_bar"), "5m")
        self.assertEqual(captured.get("clear_horizon_seconds"), 1200)
        self.assertEqual(tuple(captured.get("horizons")), (1200, 3600))
        self.assertEqual(captured.get("confirm_mid_bar"), "15m")
        self.assertEqual(captured.get("decision_ts_min"), 100.0)
        self.assertEqual(captured.get("decision_ts_max"), 200.0)


class TestPriceHorizonEntryBar(unittest.TestCase):
    def test_5m_bar_seconds_close_path(self):
        # Two 5m bars: opens 0, 300; closes 300, 600.
        candles = [
            Candle(0.0, 100, 101, 99, 100.0, 1.0),
            Candle(300.0, 100, 102, 99, 110.0, 1.0),
        ]
        found = price_at_horizon(
            candles,
            entry_ts=300.0,
            horizon_seconds=150.0,
            bar_seconds=300.0,
        )
        self.assertIsNotNone(found)
        assert found is not None
        _ts, px, src = found
        self.assertEqual(src, "interp_close")
        self.assertAlmostEqual(px, 105.0, places=5)


class TestModestMultitfGrid(unittest.TestCase):
    def test_grid_size_modest_and_cooldown_scaled(self):
        for b in ENTRY_BARS:
            grid = modest_multitf_grid(b)
            self.assertGreaterEqual(len(grid), 8)
            self.assertLessEqual(len(grid), 30)
            cds = {c.cooldown_seconds for c in grid}
            expected = set(entry_tf_spec(b).cooldown_seconds_grid())
            self.assertTrue(expected.issubset(cds))
            # Cooldowns are multiples of bar seconds.
            bs = entry_tf_spec(b).bar_seconds
            for cd in cds:
                self.assertEqual(cd % bs, 0)


if __name__ == "__main__":
    unittest.main()
