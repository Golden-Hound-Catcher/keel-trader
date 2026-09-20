"""F9: rule-side selectivity gates (soft-4h, 15m align, ADX, RSI mid)."""
from __future__ import annotations

import os
import unittest

from keel.factors.market_data import MarketSnapshot
from keel.policy import (
    diagnose_rule_signal,
    resolve_rule_4h_mode,
    resolve_rule_require_15m_align,
    rule_based_decision,
)
from keel.policy.tv_rules import adx_regime_ok


_KEYS = (
    "KEEL_RULE_VARIANT",
    "KEEL_RULE_TF_REQUIRE_4H",
    "KEEL_RULE_4H_MODE",
    "KEEL_RULE_REQUIRE_15M_ALIGN",
    "KEEL_RULE_ADX_MIN",
    "KEEL_RULE_SHORT_RSI_MAX",
    "KEEL_RULE_LONG_RSI_MIN",
    "KEEL_RULE_TF_PULLBACK",
    "KEEL_RULE_TF_MAX_EXTENSION_ATR",
    "KEEL_RULE_TF_RSI_LONG_MAX",
    "KEEL_RULE_TF_RSI_SHORT_MIN",
    "KEEL_RULE_TF_MACD_LAG_BPS",
    "KEEL_RULE_REQUIRE_1H_TREND",
    "KEEL_RULE_RSI_RELAX_ENABLE",
    "KEEL_RULE_MIN_VOLUME_RATIO",
    "KEEL_RULE_MIN_VOLUME_PERCENTILE",
    "KEEL_RULE_VOLUME_SOFT_ENABLE",
)


def _snap(**overrides) -> MarketSnapshot:
    base = dict(
        inst_id="BTC-USDT-SWAP",
        name="BTC",
        timestamp=1.0,
        price=65000.0,
        atr_14=500.0,
        rsi_14=55.0,
        trend_15m="bullish",
        trend_1h="bullish",
        trend_4h="neutral",
        macd_histogram=10.0,
        ema_9=65100.0,
        ema_21=64900.0,
        volume_ratio=1.2,
        data_valid=True,
    )
    base.update(overrides)
    return MarketSnapshot(**base)


class TestF9RuleSelectivity(unittest.TestCase):
    def _save(self):
        return {k: os.environ.get(k) for k in _KEYS}

    def _restore(self, prev):
        for k, v in prev.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v

    def _enable_tf_f9(self, **extra):
        os.environ["KEEL_RULE_VARIANT"] = "trend_follow"
        os.environ["KEEL_RULE_MIN_VOLUME_RATIO"] = "0.5"
        os.environ["KEEL_RULE_MIN_VOLUME_PERCENTILE"] = "0"
        os.environ["KEEL_RULE_VOLUME_SOFT_ENABLE"] = "0"
        os.environ["KEEL_RULE_TF_PULLBACK"] = "0"
        os.environ["KEEL_RULE_TF_REQUIRE_4H"] = "1"
        os.environ["KEEL_RULE_4H_MODE"] = "soft"
        os.environ["KEEL_RULE_REQUIRE_15M_ALIGN"] = "1"
        # Isolate ADX (fail-open path) unless a test sets a snapshot ADX.
        os.environ["KEEL_RULE_ADX_MIN"] = "0"
        os.environ["KEEL_RULE_SHORT_RSI_MAX"] = "52"
        os.environ["KEEL_RULE_LONG_RSI_MIN"] = "48"
        for k, v in extra.items():
            os.environ[k] = str(v)

    def test_soft_4h_neutral_allows_long(self):
        prev = self._save()
        try:
            self._enable_tf_f9()
            d = rule_based_decision(_snap(trend_4h="neutral", rsi_14=55.0))
            self.assertEqual(d.action, "BUY_LONG")
            self.assertEqual(d.signal_diag["rule_4h_mode"], "soft")
            self.assertEqual(d.signal_diag["trend_gate"], "15m_align+1h+4h_soft")
            self.assertTrue(d.signal_diag["trend_bullish"])
        finally:
            self._restore(prev)

    def test_soft_4h_opposing_blocks(self):
        prev = self._save()
        try:
            self._enable_tf_f9()
            d = rule_based_decision(_snap(trend_4h="bearish", rsi_14=55.0))
            self.assertEqual(d.action, "WAIT")
            self.assertIn("trend_bullish", d.signal_diag["missing"])
            self.assertIn("gates=trend_bullish", d.reason)
        finally:
            self._restore(prev)

    def test_15m_opposing_blocks_long(self):
        prev = self._save()
        try:
            self._enable_tf_f9()
            d = rule_based_decision(
                _snap(trend_15m="bearish", trend_1h="bullish", trend_4h="neutral", rsi_14=55.0)
            )
            self.assertEqual(d.action, "WAIT")
            self.assertFalse(d.signal_diag["tf15_align_long_ok"])
            self.assertIn("trend_bullish", d.signal_diag["missing"])
        finally:
            self._restore(prev)

    def test_15m_neutral_blocks_when_align_requires_same_dir(self):
        """F9 TF: REQUIRE_15M_ALIGN uses same-dir entry (neutral blocks)."""
        prev = self._save()
        try:
            self._enable_tf_f9()
            d = rule_based_decision(
                _snap(trend_15m="neutral", trend_1h="bullish", trend_4h="neutral", rsi_14=55.0)
            )
            self.assertEqual(d.action, "WAIT")
            self.assertIn("trend_bullish", d.signal_diag["missing"])
            # not-opposing stamp still true for long (neutral ≠ bearish)
            self.assertTrue(d.signal_diag["tf15_align_long_ok"])
        finally:
            self._restore(prev)

    def test_rsi_mid_veto_long_low_rsi(self):
        prev = self._save()
        try:
            self._enable_tf_f9()
            d = rule_based_decision(_snap(rsi_14=45.0, trend_4h="bullish"))
            self.assertEqual(d.action, "WAIT")
            self.assertFalse(d.signal_diag["rsi_mid_ok"])
            self.assertIn("rsi_mid_ok", d.signal_diag["missing"])
            self.assertIn("gates=", d.reason)
        finally:
            self._restore(prev)

    def test_rsi_mid_veto_short_high_rsi(self):
        prev = self._save()
        try:
            self._enable_tf_f9()
            d = rule_based_decision(
                _snap(
                    rsi_14=55.0,
                    trend_15m="bearish",
                    trend_1h="bearish",
                    trend_4h="neutral",
                    macd_histogram=-10.0,
                    ema_9=64900.0,
                    ema_21=65100.0,
                )
            )
            self.assertEqual(d.action, "WAIT")
            self.assertIn("rsi_mid_ok", d.signal_diag["missing"])
        finally:
            self._restore(prev)

    def test_rsi_mid_pass_band(self):
        prev = self._save()
        try:
            self._enable_tf_f9()
            d = rule_based_decision(_snap(rsi_14=55.0, trend_4h="bullish"))
            self.assertEqual(d.action, "BUY_LONG")
            self.assertTrue(d.signal_diag["rsi_mid_ok"])
        finally:
            self._restore(prev)

    def test_adx_floor_blocks_when_present(self):
        prev = self._save()
        try:
            self._enable_tf_f9(KEEL_RULE_ADX_MIN="15")
            snap = _snap(rsi_14=55.0, trend_4h="bullish")
            snap.adx_14 = 10.0  # type: ignore[attr-defined]
            d = rule_based_decision(snap)
            self.assertEqual(d.action, "WAIT")
            self.assertFalse(d.signal_diag["adx_ok"])
            self.assertIn("adx_ok", d.signal_diag["missing"])
            self.assertFalse(d.signal_diag.get("adx_fail_open"))
        finally:
            self._restore(prev)

    def test_adx_fail_open_when_unavailable(self):
        prev = self._save()
        try:
            self._enable_tf_f9(KEEL_RULE_ADX_MIN="15")
            # No candles / no adx attr → fail-open.
            d = rule_based_decision(_snap(rsi_14=55.0, trend_4h="bullish"))
            self.assertEqual(d.action, "BUY_LONG")
            self.assertTrue(d.signal_diag["adx_ok"])
            self.assertTrue(d.signal_diag.get("adx_fail_open"))
        finally:
            self._restore(prev)

    def test_adx_regime_ok_fail_open_helper(self):
        prev = self._save()
        try:
            os.environ["KEEL_RULE_ADX_MIN"] = "15"
            info = adx_regime_ok(_snap())
            self.assertTrue(info["adx_enabled"])
            self.assertTrue(info["adx_ok"])
            self.assertTrue(info["adx_fail_open"])
        finally:
            self._restore(prev)

    def test_resolve_helpers(self):
        prev = self._save()
        try:
            for k in _KEYS:
                os.environ.pop(k, None)
            os.environ["KEEL_RULE_VARIANT"] = "trend_follow"
            self.assertEqual(resolve_rule_4h_mode(), "hard")  # F9.1: rules default hard
            self.assertTrue(resolve_rule_require_15m_align())
            os.environ["KEEL_RULE_4H_MODE"] = "hard"
            self.assertEqual(resolve_rule_4h_mode(), "hard")
            os.environ["KEEL_RULE_TF_REQUIRE_4H"] = "0"
            self.assertEqual(resolve_rule_4h_mode(), "off")
        finally:
            self._restore(prev)

    def test_mean_revert_ignores_f9_gates(self):
        prev = self._save()
        try:
            for k in _KEYS:
                os.environ.pop(k, None)
            os.environ["KEEL_RULE_4H_MODE"] = "soft"
            os.environ["KEEL_RULE_REQUIRE_15M_ALIGN"] = "1"
            os.environ["KEEL_RULE_SHORT_RSI_MAX"] = "52"
            os.environ["KEEL_RULE_LONG_RSI_MIN"] = "48"
            diag = diagnose_rule_signal(_snap(rsi_14=50.0))
            self.assertEqual(diag["rule_variant"], "mean_revert")
            self.assertFalse(diag.get("rsi_mid_enabled"))
            self.assertEqual(diag.get("rule_4h_mode"), "off")
        finally:
            self._restore(prev)


if __name__ == "__main__":
    unittest.main()
