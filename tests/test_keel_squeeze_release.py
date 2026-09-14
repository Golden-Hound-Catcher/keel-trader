"""P5 squeeze-release event entry (offline, no network)."""
from __future__ import annotations

import os
import unittest

from keel.factors.market_data import MarketSnapshot
from keel.policy.stub import diagnose_rule_signal, resolve_rule_variant, rule_based_decision


class TestSqueezeReleaseVariant(unittest.TestCase):
    _KEYS = (
        "KEEL_RULE_VARIANT",
        "KEEL_RULE_TF_REQUIRE_4H",
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

    def _enable(self) -> None:
        os.environ["KEEL_RULE_VARIANT"] = "squeeze_release"
        os.environ["KEEL_RULE_TF_REQUIRE_4H"] = "0"
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
            trend_1h="bullish",
            trend_4h="neutral",
            macd_histogram=0.0,
            ema_9=65000.0,
            ema_21=65000.0,
            volume_ratio=1.2,
            data_valid=True,
            squeeze=False,
            squeeze_prev=True,
            squeeze_release=True,
            supertrend_direction=1,
            regime="trend",
        )
        data.update(overrides)
        return MarketSnapshot(**data)  # type: ignore[arg-type]

    def test_resolve_variant_aliases(self):
        self._enable()
        self.assertEqual(resolve_rule_variant(), "squeeze_release")
        os.environ["KEEL_RULE_VARIANT"] = "sqz"
        self.assertEqual(resolve_rule_variant(), "squeeze_release")
        os.environ["KEEL_RULE_VARIANT"] = "squeeze"
        self.assertEqual(resolve_rule_variant(), "squeeze_release")

    def test_still_squeezed_waits(self):
        self._enable()
        d = rule_based_decision(
            self._base(
                squeeze=True,
                squeeze_prev=True,
                squeeze_release=False,
                supertrend_direction=1,
                trend_1h="bullish",
            )
        )
        self.assertEqual(d.action, "WAIT")
        self.assertEqual(d.signal_diag["missing"], ["squeeze_release"])
        self.assertIn("squeeze_release", d.reason)

    def test_release_st_htf_fires_long(self):
        self._enable()
        d = rule_based_decision(self._base())
        self.assertEqual(d.action, "BUY_LONG")
        self.assertEqual(d.signal_diag["missing"], [])
        self.assertEqual(d.signal_diag["nearest"], "long")
        self.assertTrue(d.signal_diag["squeeze_release"])

    def test_release_st_htf_fires_short(self):
        self._enable()
        d = rule_based_decision(
            self._base(
                trend_1h="bearish",
                supertrend_direction=-1,
            )
        )
        self.assertEqual(d.action, "SELL_SHORT")
        self.assertEqual(d.signal_diag["nearest"], "short")

    def test_missing_supertrend_waits(self):
        self._enable()
        d = rule_based_decision(self._base(supertrend_direction=0))
        self.assertEqual(d.action, "WAIT")
        self.assertIn("st_ok", d.signal_diag["missing"])

    def test_htf_disagree_waits(self):
        self._enable()
        d = rule_based_decision(self._base(trend_1h="bearish", supertrend_direction=1))
        self.assertEqual(d.action, "WAIT")
        self.assertIn("htf_ok", d.signal_diag["missing"])

    def test_rsi_chase_veto(self):
        self._enable()
        d = rule_based_decision(self._base(rsi_14=75.0))
        self.assertEqual(d.action, "WAIT")
        self.assertIn("rsi_veto", d.signal_diag["missing"])

    def test_mean_revert_ignores_release_fields(self):
        os.environ["KEEL_RULE_VARIANT"] = "mean_revert"
        d = diagnose_rule_signal(self._base(rsi_14=50.0, trend_15m="neutral"))
        self.assertEqual(d["rule_variant"], "mean_revert")
        self.assertNotEqual(d.get("regime_path"), "squeeze_release")
