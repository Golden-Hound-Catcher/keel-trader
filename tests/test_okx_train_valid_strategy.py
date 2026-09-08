"""F3: train/valid split boundaries + selection uses train metrics only."""
from __future__ import annotations

import unittest
from unittest.mock import patch

from keel.backtest.train_valid import (
    MIN_TRAIN_AVG_NET_RT_BPS,
    MIN_TRAIN_FG,
    StrategyConfig,
    decision_bounds_ok,
    extract_train_metrics,
    make_train_valid_windows,
    meets_selection_gates,
    modest_strategy_grid,
    rank_train_rows,
    select_primary_strategy,
    series_for_train_markout,
)
from keel.backtest.okx_history_rule import HistorySeries
from keel.factors.market_data import Candle


class TestSplitBoundaries(unittest.TestCase):
    def test_windows_are_half_open_14d_7d(self):
        now = 1_700_000_000.0
        w = make_train_valid_windows(now)
        self.assertEqual(w.now_ts, now)
        self.assertEqual(w.valid_end_ts, now)
        self.assertEqual(w.valid_start_ts, now - 7 * 86400.0)
        self.assertEqual(w.train_end_ts, w.valid_start_ts)
        self.assertEqual(w.train_start_ts, now - 14 * 86400.0)
        # Boundary: train_end is exclusive for train, inclusive for valid start.
        self.assertTrue(
            decision_bounds_ok(
                w.train_start_ts,
                window_start=w.train_start_ts,
                window_end=w.train_end_ts,
            )
        )
        self.assertFalse(
            decision_bounds_ok(
                w.train_end_ts,
                window_start=w.train_start_ts,
                window_end=w.train_end_ts,
            )
        )
        self.assertTrue(
            decision_bounds_ok(
                w.valid_start_ts,
                window_start=w.valid_start_ts,
                window_end=w.valid_end_ts,
            )
        )
        self.assertFalse(
            decision_bounds_ok(
                w.valid_end_ts,
                window_start=w.valid_start_ts,
                window_end=w.valid_end_ts,
            )
        )

    def test_train_truncate_drops_valid_opens(self):
        train_end = 1_700_000_000.0
        candles = [
            Candle(train_end - 1800.0, 1, 1, 1, 1, 1),
            Candle(train_end - 900.0, 1, 1, 1, 1, 1),
            Candle(train_end, 1, 1, 1, 1, 1),  # open at boundary → drop
            Candle(train_end + 900.0, 1, 1, 1, 1, 1),
        ]
        series = HistorySeries(
            inst_id="BTC-USDT-SWAP",
            candles_15m=candles,
            candles_1h=list(candles),
            candles_4h=list(candles),
        )
        trunc = series_for_train_markout(series, train_end_ts=train_end)
        self.assertEqual(len(trunc.candles_15m), 2)
        self.assertTrue(all(c.timestamp < train_end for c in trunc.candles_15m))


class TestSelectionTrainOnly(unittest.TestCase):
    def _row(
        self,
        label: str,
        *,
        fg: int,
        win: float | None,
        avg: float | None,
        valid_win: float | None = 0.99,
    ) -> dict:
        cfg = StrategyConfig(
            variant="trend_follow",
            require_4h=True,
            max_extension_atr=0.0,
            pullback=False,
            rsi_pullback_long_max=None,
            rsi_pullback_short_min=None,
            cooldown_seconds=900,
        )
        # Stuff distinct cooldown so labels differ if needed.
        if "cd1800" in label:
            cfg = StrategyConfig(
                variant="trend_follow",
                require_4h=True,
                max_extension_atr=0.0,
                pullback=False,
                rsi_pullback_long_max=None,
                rsi_pullback_short_min=None,
                cooldown_seconds=1800,
            )
        return {
            "config": cfg,
            "train": {
                "full_gate_count": fg,
                "win_rate_net_rt_5m": win,
                "avg_net_rt_bps_5m": avg,
                "frac_clear_10bps_5m": 0.0,
            },
            # If selection peeked at valid, it would pick this high valid win.
            "valid_leaked": valid_win,
            "gates_ok": meets_selection_gates(
                {
                    "full_gate_count": fg,
                    "avg_net_rt_bps_5m": avg,
                }
            ),
        }

    def test_selects_max_train_win_among_gate_passers(self):
        rows = [
            self._row("low", fg=20, win=0.20, avg=0.0, valid_win=0.99),
            self._row("cd1800_best_train", fg=15, win=0.40, avg=-1.0, valid_win=0.01),
            self._row("mid", fg=12, win=0.30, avg=1.0, valid_win=0.80),
        ]
        # Rank as the CLI would (train only).
        ranked = rank_train_rows(rows)
        chosen, note = select_primary_strategy(ranked)
        self.assertIsNotNone(chosen)
        assert chosen is not None
        self.assertEqual(chosen["config"].cooldown_seconds, 1800)
        self.assertIn("pre-declared", note)
        # Must not prefer the 0.99 valid_leaked row.
        self.assertNotEqual(chosen["train"]["win_rate_net_rt_5m"], 0.20)

    def test_fallback_when_no_gates(self):
        rows = [
            self._row("a", fg=3, win=0.50, avg=-20.0, valid_win=0.99),
            self._row("cd1800", fg=8, win=0.10, avg=-1.0, valid_win=0.01),
        ]
        ranked = rank_train_rows(rows)
        chosen, note = select_primary_strategy(ranked)
        self.assertIsNotNone(chosen)
        assert chosen is not None
        self.assertEqual(chosen["train"]["win_rate_net_rt_5m"], 0.50)
        self.assertIn("best available", note)

    def test_selection_ignores_valid_keys(self):
        """Guard: even if a row carries valid metrics, selection reads train only."""
        rows = [
            {
                "config": StrategyConfig(
                    variant="trend_follow",
                    require_4h=True,
                    max_extension_atr=1.0,
                    pullback=False,
                    rsi_pullback_long_max=None,
                    rsi_pullback_short_min=None,
                    cooldown_seconds=900,
                ),
                "train": {
                    "full_gate_count": 20,
                    "win_rate_net_rt_5m": 0.25,
                    "avg_net_rt_bps_5m": 0.0,
                },
                "valid": {"win_rate_net_rt_5m": 0.99},
            },
            {
                "config": StrategyConfig(
                    variant="trend_follow",
                    require_4h=True,
                    max_extension_atr=2.0,
                    pullback=False,
                    rsi_pullback_long_max=None,
                    rsi_pullback_short_min=None,
                    cooldown_seconds=1800,
                ),
                "train": {
                    "full_gate_count": 20,
                    "win_rate_net_rt_5m": 0.45,
                    "avg_net_rt_bps_5m": 1.0,
                },
                "valid": {"win_rate_net_rt_5m": 0.05},
            },
        ]
        chosen, _ = select_primary_strategy(rank_train_rows(rows))
        assert chosen is not None
        self.assertEqual(chosen["config"].max_extension_atr, 2.0)
        self.assertEqual(chosen["train"]["win_rate_net_rt_5m"], 0.45)

    def test_grid_is_modest(self):
        grid = modest_strategy_grid()
        self.assertGreaterEqual(len(grid), 20)
        self.assertLessEqual(len(grid), 60)
        # Includes both families.
        variants = {c.variant for c in grid}
        self.assertIn("trend_follow", variants)
        self.assertIn("mean_revert", variants)


class TestWalkWindowFilter(unittest.TestCase):
    def test_decision_window_kwargs_forwarded(self):
        """walk_forward receives decision_ts_min/max (no peek via full walk)."""
        from keel.backtest import train_valid as tv

        cfg = StrategyConfig(
            variant="trend_follow",
            require_4h=True,
            max_extension_atr=0.0,
            pullback=False,
            rsi_pullback_long_max=None,
            rsi_pullback_short_min=None,
            cooldown_seconds=900,
        )
        captured: dict = {}

        def _fake_walk(*_a, **kwargs):
            captured.update(kwargs)
            return {
                "full_gate_count": 0,
                "full_gate_rate": 0.0,
                "n_steps": 0,
                "markout": {},
            }

        with patch.object(tv, "walk_forward_backtest", side_effect=_fake_walk):
            tv.run_config_on_window(
                [],
                cfg,
                decision_ts_min=100.0,
                decision_ts_max=200.0,
            )
        self.assertEqual(captured.get("decision_ts_min"), 100.0)
        self.assertEqual(captured.get("decision_ts_max"), 200.0)
        self.assertEqual(captured.get("variant"), "trend_follow")


class TestExtractMetrics(unittest.TestCase):
    def test_extract(self):
        summary = {
            "full_gate_count": 11,
            "full_gate_rate": 0.1,
            "n_steps": 100,
            "markout": {
                "win_rate_net_rt_5m": 0.3,
                "avg_net_rt_bps_5m": -2.0,
                "frac_clear_10bps_5m": 0.0,
                "by_horizon": {"900": {"win_rate_net_rt": 0.2}},
            },
        }
        m = extract_train_metrics(summary)
        self.assertEqual(m["full_gate_count"], 11)
        self.assertEqual(m["win_rate_net_rt_5m"], 0.3)
        self.assertTrue(meets_selection_gates(m))
        self.assertGreaterEqual(MIN_TRAIN_FG, 10)
        self.assertEqual(MIN_TRAIN_AVG_NET_RT_BPS, -5.0)


if __name__ == "__main__":
    unittest.main()
