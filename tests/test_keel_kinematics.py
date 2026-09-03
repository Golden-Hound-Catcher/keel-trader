"""Tests for keel.factors.kinematics (ported pure math, honest names)."""
from __future__ import annotations

import unittest
import warnings

from keel.factors.kinematics import (
    calculate_multi_timeframe_kinematics,
    calculate_path_integrals,
    calculate_price_kinematics,
    calculate_return_statistics,
    classify_kinematic_regime,
    classify_path_energy_regime,
    clip_normalise,
    diff_series,
    ema_series,
    normal_cdf,
)


class TestKinematicsHelpers(unittest.TestCase):
    def test_ema_series_and_diff(self):
        series = ema_series([1.0, 2.0, 3.0], span=2)
        self.assertEqual(len(series), 3)
        self.assertEqual(diff_series([1.0, 3.0, 6.0]), [2.0, 3.0])

    def test_clip_and_cdf(self):
        self.assertEqual(clip_normalise(10.0, 1.0, bound=3.0), 3.0)
        self.assertAlmostEqual(normal_cdf(0.0), 0.5, places=5)


class TestPriceKinematics(unittest.TestCase):
    def test_bullish_series(self):
        prices = [100.0, 101.0, 103.0, 106.0, 110.0, 115.0, 122.0, 131.0, 142.0, 155.0]
        res = calculate_price_kinematics(prices)
        self.assertTrue(res["valid"])
        self.assertGreater(res["velocity"], 0.0)
        self.assertGreater(res["impulse"], 0.0)
        self.assertEqual(res["direction"], 1)
        self.assertIn("definite_integrals", res)
        self.assertIn("probability_theory", res)

    def test_insufficient_data(self):
        res = calculate_price_kinematics([100.0, 101.0])
        self.assertFalse(res["valid"])

    def test_path_integrals(self):
        prices = [100 + i for i in range(15)]
        integ = calculate_path_integrals(prices)
        self.assertTrue(integ["valid"])
        self.assertIn("energy_integral", integ)

    def test_return_statistics(self):
        returns = [0.01, -0.005, 0.02, -0.01, 0.015, 0.0, 0.008]
        stats = calculate_return_statistics(returns, velocity=0.5, acceleration=0.1)
        self.assertTrue(stats["valid"])
        self.assertIn("skewness", stats)
        self.assertIn("var_95_pct", stats)

    def test_multi_timeframe_okx_newest_first(self):
        rows = [[str(i), "101", "99", str(100 + i), "1"] for i in range(10)]
        result = calculate_multi_timeframe_kinematics({"15M": list(reversed(rows))})
        self.assertTrue(result["valid"])
        self.assertGreater(result["timeframes"]["15M"]["velocity"], 0)

    def test_regime_classifiers(self):
        self.assertEqual(
            classify_kinematic_regime(0.0, 0.0, 0.0, 0.0),
            "RANGE_LOW_VELOCITY",
        )
        self.assertEqual(classify_path_energy_regime(0.0, 0.0), "BALANCED_ENERGY")


class TestCalculusShim(unittest.TestCase):
    def test_shim_reexports(self):
        import importlib
        import sys
        from pathlib import Path

        scripts = str(Path(__file__).resolve().parents[1] / "scripts")
        if scripts not in sys.path:
            sys.path.insert(0, scripts)
        sys.modules.pop("calculus_engine", None)
        with warnings.catch_warnings(record=True) as w:
            warnings.simplefilter("always")
            import calculus_engine as ce

            self.assertTrue(any(issubclass(x.category, DeprecationWarning) for x in w))
        base = [100, 101, 102, 104, 107, 111, 116, 122, 129, 137]
        a = ce.calculate_calculus(base)
        self.assertTrue(a["valid"])
        self.assertIs(ce.calculate_calculus, calculate_price_kinematics)


if __name__ == "__main__":
    unittest.main()
