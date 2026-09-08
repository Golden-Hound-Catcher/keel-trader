"""F0b: offline OKX history rule backtest (synthetic, no network)."""
from __future__ import annotations

import os
import unittest
from unittest.mock import patch

from keel.backtest.okx_history_rule import (
    build_snapshot_at,
    fee_aware_markouts,
    forced_e31_rule_env,
    price_at_horizon,
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


class TestRequire4hPath(unittest.TestCase):
    def tearDown(self) -> None:
        for k in (
            "KEEL_RULE_VARIANT",
            "KEEL_RULE_TF_REQUIRE_4H",
            "KEEL_RULE_TF_MACD_LAG_BPS",
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


if __name__ == "__main__":
    unittest.main()
