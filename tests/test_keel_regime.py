"""P1 regime-router rule variant (offline, no network)."""
from __future__ import annotations

import os
import unittest

from keel.domain.decision import Decision
from keel.factors.market_data import MarketSnapshot
from keel.policy.stub import diagnose_rule_signal, rule_based_decision, resolve_rule_variant


class TestRegimeVariant(unittest.TestCase):
    _KEYS = (
        "KEEL_RULE_VARIANT",
        "KEEL_RULE_TF_REQUIRE_4H",
        "KEEL_RULE_MIN_VOLUME_RATIO",
        "KEEL_RULE_MIN_VOLUME_PERCENTILE",
        "KEEL_RULE_VOLUME_SOFT_ENABLE",
        "KEEL_RULE_TF_PULLBACK",
    )

    def setUp(self) -> None:
        self._prev = {k: os.environ.get(k) for k in self._KEYS}

    def tearDown(self) -> None:
        for k, v in self._prev.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v

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

    def test_resolve_variant_regime(self):
        self._enable_regime()
        self.assertEqual(resolve_rule_variant(), "regime")

    def test_squeeze_waits(self):
        self._enable_regime()
        d = rule_based_decision(self._base(regime="squeeze"))
        self.assertEqual(d.action, "WAIT")
        self.assertEqual(d.signal_diag["regime"], "squeeze")
        self.assertEqual(d.signal_diag["missing"], ["regime_ok"])
        self.assertIn("regime squeeze", d.reason)

    def test_shock_waits(self):
        self._enable_regime()
        d = rule_based_decision(self._base(regime="shock"))
        self.assertEqual(d.action, "WAIT")
        self.assertEqual(d.signal_diag["missing"], ["regime_ok"])

    def test_range_long_fade_fires(self):
        self._enable_regime()
        d = rule_based_decision(
            self._base(
                regime="range",
                bb_percent_b=0.10,
                vwap_bias_pct=-0.4,
                rsi_14=42.0,
                volume_ratio=1.1,
            )
        )
        self.assertEqual(d.action, "BUY_LONG")
        self.assertEqual(d.signal_diag["missing"], [])
        self.assertEqual(d.signal_diag["nearest"], "long")
        self.assertEqual(d.signal_diag["regime_path"], "range")
        self.assertIsInstance(d, Decision)
        self.assertIn("regime range", d.reason)

    def test_range_short_fade_fires(self):
        self._enable_regime()
        d = rule_based_decision(
            self._base(
                regime="range",
                bb_percent_b=0.90,
                vwap_bias_pct=0.5,
                rsi_14=58.0,
                volume_ratio=1.1,
            )
        )
        self.assertEqual(d.action, "SELL_SHORT")
        self.assertEqual(d.signal_diag["missing"], [])
        self.assertEqual(d.signal_diag["nearest"], "short")

    def test_range_mid_band_waits(self):
        self._enable_regime()
        d = rule_based_decision(
            self._base(regime="range", bb_percent_b=0.5, vwap_bias_pct=0.0, rsi_14=50.0)
        )
        self.assertEqual(d.action, "WAIT")
        self.assertIn("range_pb_ok", d.signal_diag["missing"])

    def test_trend_path_tf_like_long(self):
        self._enable_regime()
        d = rule_based_decision(
            self._base(
                regime="trend",
                trend_15m="bullish",
                trend_1h="bullish",
                trend_4h="neutral",
                rsi_14=50.0,
                macd_histogram=8.0,
                ema_9=65100.0,
                ema_21=64900.0,
                volume_ratio=1.2,
                supertrend_direction=1,
            )
        )
        self.assertEqual(d.action, "BUY_LONG")
        self.assertEqual(d.signal_diag["missing"], [])
        self.assertEqual(d.signal_diag["regime"], "trend")
        self.assertEqual(d.signal_diag["trend_gate"], "15m+1h")
        self.assertFalse(d.signal_diag["require_4h_trend"])
        self.assertIn("regime trend", d.reason)

    def test_mean_revert_unchanged_when_not_regime(self):
        os.environ.pop("KEEL_RULE_VARIANT", None)
        d = diagnose_rule_signal(self._base(regime="squeeze", rsi_14=50.0))
        self.assertEqual(d["rule_variant"], "mean_revert")
        self.assertNotEqual(d.get("missing"), ["regime_ok"])


if __name__ == "__main__":
    unittest.main()
