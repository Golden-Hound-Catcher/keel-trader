"""P2 scored regime-router (offline, no network)."""
from __future__ import annotations

import os
import unittest

from keel.factors.market_data import MarketSnapshot
from keel.policy.stub import diagnose_rule_signal, resolve_rule_variant, rule_based_decision


class TestScoreVariant(unittest.TestCase):
    _KEYS = (
        "KEEL_RULE_VARIANT",
        "KEEL_RULE_TF_REQUIRE_4H",
        "KEEL_RULE_MIN_VOLUME_RATIO",
        "KEEL_RULE_MIN_VOLUME_PERCENTILE",
        "KEEL_RULE_VOLUME_SOFT_ENABLE",
        "KEEL_RULE_TF_PULLBACK",
        "KEEL_RULE_SCORE_MIN",
        "KEEL_RULE_RANGE_VOLUME_SOFT",
    )

    def setUp(self) -> None:
        self._prev = {k: os.environ.get(k) for k in self._KEYS}

    def tearDown(self) -> None:
        for k, v in self._prev.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v

    def _enable_score(self) -> None:
        os.environ["KEEL_RULE_VARIANT"] = "score"
        os.environ["KEEL_RULE_TF_REQUIRE_4H"] = "0"
        os.environ["KEEL_RULE_MIN_VOLUME_RATIO"] = "0.5"
        os.environ["KEEL_RULE_MIN_VOLUME_PERCENTILE"] = "0"
        os.environ["KEEL_RULE_VOLUME_SOFT_ENABLE"] = "1"
        os.environ["KEEL_RULE_TF_PULLBACK"] = "0"
        os.environ["KEEL_RULE_RANGE_VOLUME_SOFT"] = "0"

    def _enable_regime(self) -> None:
        os.environ["KEEL_RULE_VARIANT"] = "regime"
        os.environ["KEEL_RULE_TF_REQUIRE_4H"] = "0"
        os.environ["KEEL_RULE_MIN_VOLUME_RATIO"] = "0.5"
        os.environ["KEEL_RULE_MIN_VOLUME_PERCENTILE"] = "0"
        os.environ["KEEL_RULE_VOLUME_SOFT_ENABLE"] = "1"
        os.environ["KEEL_RULE_TF_PULLBACK"] = "0"

    def _base(self, **overrides) -> MarketSnapshot:
        data = dict(
            inst_id="BTC-USDT-SWAP",
            name="BTC",
            timestamp=1.0,
            price=65000.0,
            atr_14=400.0,
            rsi_14=45.0,
            trend_15m="neutral",
            trend_1h="neutral",
            trend_4h="neutral",
            macd_histogram=0.0,
            ema_9=65000.0,
            ema_21=65000.0,
            volume_ratio=1.2,
            data_valid=True,
            bb_percent_b=0.5,
            vwap_bias_pct=0.0,
            squeeze=False,
            supertrend_direction=0,
            regime="range",
        )
        data.update(overrides)
        return MarketSnapshot(**data)  # type: ignore[arg-type]

    def test_resolve_variant_score(self):
        self._enable_score()
        self.assertEqual(resolve_rule_variant(), "score")
        os.environ["KEEL_RULE_VARIANT"] = "regime_score"
        self.assertEqual(resolve_rule_variant(), "score")

    def test_squeeze_still_waits(self):
        self._enable_score()
        d = rule_based_decision(self._base(regime="squeeze"))
        self.assertEqual(d.action, "WAIT")
        self.assertEqual(d.signal_diag["missing"], ["regime_ok"])

    def test_trend_fires_at_score_4_without_volume(self):
        self._enable_score()
        d = rule_based_decision(
            self._base(
                regime="trend",
                trend_1h="bullish",
                supertrend_direction=1,
                ema_21=65000.0,
                vwap_bias_pct=-0.2,
                macd_histogram=8.0,
                volume_ratio=0.1,
                rsi_14=52.0,
            )
        )
        self.assertEqual(d.action, "BUY_LONG")
        self.assertGreaterEqual(int(d.signal_diag["score"]), 4)
        self.assertEqual(d.signal_diag["missing"], [])
        self.assertFalse(d.signal_diag["score_breakdown"]["volume"])
        self.assertIn("score=4/4", d.reason)

    def test_trend_rsi_chase_vetoes(self):
        self._enable_score()
        d = rule_based_decision(
            self._base(
                regime="trend",
                trend_1h="bullish",
                supertrend_direction=1,
                vwap_bias_pct=-0.2,
                macd_histogram=8.0,
                volume_ratio=1.2,
                rsi_14=78.0,
            )
        )
        self.assertEqual(d.action, "WAIT")
        self.assertIn("rsi_veto", d.signal_diag["missing"])
        self.assertFalse(d.signal_diag["rsi_veto_ok"])

    def test_trend_without_supertrend_does_not_fire(self):
        self._enable_score()
        d = rule_based_decision(
            self._base(
                regime="trend",
                trend_1h="bullish",
                supertrend_direction=0,
                vwap_bias_pct=-0.2,
                macd_histogram=8.0,
                volume_ratio=1.2,
                rsi_14=50.0,
            )
        )
        self.assertEqual(d.action, "WAIT")
        self.assertIn("st", d.signal_diag["missing"])

    def test_trend_low_score_waits(self):
        self._enable_score()
        d = rule_based_decision(
            self._base(
                regime="trend",
                trend_1h="neutral",
                supertrend_direction=0,
                vwap_bias_pct=0.8,
                macd_histogram=-4.0,
                volume_ratio=0.1,
                rsi_14=50.0,
            )
        )
        self.assertEqual(d.action, "WAIT")
        self.assertLess(int(d.signal_diag["score"]), 4)
        self.assertIn("score_ok", d.signal_diag["missing"])

    def test_range_extreme_fires(self):
        self._enable_score()
        d = rule_based_decision(
            self._base(
                regime="range",
                bb_percent_b=0.08,
                vwap_bias_pct=-0.4,
                rsi_14=35.0,
                volume_ratio=1.1,
                supertrend_direction=0,
            )
        )
        self.assertEqual(d.action, "BUY_LONG")
        self.assertEqual(d.signal_diag["regime_path"], "range")
        self.assertGreaterEqual(int(d.signal_diag["score"]), 4)
        self.assertEqual(d.signal_diag["missing"], [])

    def test_range_loose_p1_setup_does_not_fire_on_score(self):
        """Pre-declared tighter %B/RSI: P1 0.20/50 fire must not leak into P2."""
        snap = self._base(
            regime="range",
            bb_percent_b=0.18,
            vwap_bias_pct=-0.2,
            rsi_14=48.0,
            volume_ratio=1.1,
            supertrend_direction=0,
        )
        self._enable_regime()
        p1 = rule_based_decision(snap)
        self.assertEqual(p1.action, "BUY_LONG")
        self._enable_score()
        p2 = rule_based_decision(snap)
        self.assertEqual(p2.action, "WAIT")
        self.assertIn("range_pb_ok", p2.signal_diag["missing"])

    def test_regime_variant_unchanged_by_score_code(self):
        self._enable_regime()
        d = diagnose_rule_signal(
            self._base(regime="range", bb_percent_b=0.10, vwap_bias_pct=-0.3, rsi_14=42.0)
        )
        self.assertEqual(d["rule_variant"], "regime")
        self.assertNotIn("score_min", d)
