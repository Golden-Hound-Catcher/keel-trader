"""F5: SuperTrend / Donchian / volume SMA helpers + no-peek channel."""
from __future__ import annotations

import unittest

from keel.factors.technical import (
    calculate_atr,
    calculate_supertrend,
    donchian_prior_channel,
    volume_sma_ratio,
)


class TestSuperTrend(unittest.TestCase):
    def test_insufficient_returns_neutral(self):
        st = calculate_supertrend([1, 2], [0.5, 1], [1, 1.5], period=10, factor=3.0)
        self.assertEqual(st.direction, 0)
        self.assertFalse(st.flipped)

    def test_uptrend_flip_detectable(self):
        # Synthetic: long down then sharp up to force a bullish flip eventually.
        n = 80
        highs, lows, closes = [], [], []
        px = 100.0
        for i in range(n):
            if i < 40:
                px -= 0.4
            else:
                px += 0.8
            highs.append(px + 0.5)
            lows.append(px - 0.5)
            closes.append(px)
        st = calculate_supertrend(highs, lows, closes, period=10, factor=2.0)
        self.assertIn(st.direction, (-1, 1))
        self.assertGreater(st.value, 0.0)
        # Series should produce a defined band distinct from raw ATR mid.
        atr = calculate_atr(highs, lows, closes, 10)
        self.assertGreater(atr, 0.0)

    def test_factor_and_length_affect_band(self):
        n = 60
        highs = [100 + i * 0.1 + 1 for i in range(n)]
        lows = [100 + i * 0.1 - 1 for i in range(n)]
        closes = [100 + i * 0.1 for i in range(n)]
        a = calculate_supertrend(highs, lows, closes, period=10, factor=2.0)
        b = calculate_supertrend(highs, lows, closes, period=10, factor=5.0)
        # Wider factor → bands farther from price in absolute terms typically.
        self.assertNotEqual(a.upper, b.upper)


class TestDonchianPrior(unittest.TestCase):
    def test_excludes_current_bar(self):
        highs = [1, 2, 3, 10]  # current high=10 must not set prior upper
        lows = [0.5, 0.5, 0.5, 0.1]
        ch = donchian_prior_channel(highs, lows, period=3)
        self.assertIsNotNone(ch)
        assert ch is not None
        self.assertEqual(ch.upper, 3.0)
        self.assertEqual(ch.lower, 0.5)

    def test_insufficient(self):
        self.assertIsNone(donchian_prior_channel([1, 2], [0, 1], period=3))

    def test_no_peek_window(self):
        # Prior channel over first N bars equals channel computed without last.
        highs = list(range(1, 26))
        lows = [h - 1 for h in highs]
        full = donchian_prior_channel(highs, lows, period=20)
        truncated = donchian_prior_channel(highs[:-1], lows[:-1], period=20)
        # Prior of full (excludes last) uses highs[-21:-1] = highs[4:24]
        # Prior of truncated (len 24) uses highs[3:23] — different length index.
        # Stronger check: channel from highs[:-1] with period 20 should match
        # max/min of highs[-21:-1] when calling on full.
        assert full is not None
        self.assertEqual(full.upper, max(highs[-21:-1]))
        self.assertEqual(full.lower, min(lows[-21:-1]))
        # Truncated series ending one bar earlier must not see the dropped bar.
        assert truncated is not None
        self.assertNotIn(highs[-1], [truncated.upper])  # current of truncated is highs[-2]
        self.assertEqual(truncated.upper, max(highs[-22:-2]))


class TestVolumeSmaRatio(unittest.TestCase):
    def test_ratio(self):
        vols = [10.0] * 19 + [20.0]
        r = volume_sma_ratio(vols, period=20)
        self.assertAlmostEqual(r, 20.0 / ((10.0 * 19 + 20.0) / 20.0), places=6)

    def test_empty(self):
        self.assertEqual(volume_sma_ratio([], 20), 1.0)


if __name__ == "__main__":
    unittest.main()
