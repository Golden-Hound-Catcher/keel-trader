"""Q1: read-only arming checklist (mock capability; no network / no kill-switch writes)."""
from __future__ import annotations

import unittest
from types import SimpleNamespace

from keel.risk.arming import evaluate_arming


def _settings(**kwargs):
    base = dict(
        okx_configured=True,
        okx_environment="live",
        kill_switch=True,
        max_notional_per_instrument=2000.0,
        max_daily_loss_usdt=150.0,
    )
    base.update(kwargs)
    return SimpleNamespace(**base)


class TestEvaluateArmingReady(unittest.TestCase):
    def test_ready_when_trade_keys_and_limits(self):
        r = evaluate_arming(_settings(), "trade")
        self.assertTrue(r.ready_to_arm)
        self.assertEqual(r.capability, "trade")
        self.assertTrue(r.kill_switch)
        self.assertEqual(r.blockers, [])
        self.assertTrue(any("kill_switch is ON" in w for w in r.warnings))
        self.assertTrue(any("KEEL_KILL_SWITCH=0" in w for w in r.warnings))

    def test_ready_on_demo_with_warning(self):
        r = evaluate_arming(_settings(okx_environment="demo"), "trade")
        self.assertTrue(r.ready_to_arm)
        self.assertTrue(any("demo" in w for w in r.warnings))


class TestEvaluateArmingBlocked(unittest.TestCase):
    def test_blocked_when_capability_read(self):
        r = evaluate_arming(_settings(), "read")
        self.assertFalse(r.ready_to_arm)
        self.assertTrue(any("trade" in b for b in r.blockers))

    def test_blocked_when_capability_none(self):
        r = evaluate_arming(_settings(okx_configured=False), "none")
        self.assertFalse(r.ready_to_arm)
        self.assertTrue(any("keys not configured" in b for b in r.blockers))
        self.assertTrue(any("trade" in b for b in r.blockers))

    def test_blocked_when_capability_paper(self):
        r = evaluate_arming(_settings(), "paper")
        self.assertFalse(r.ready_to_arm)
        self.assertIn("paper", " ".join(r.blockers))

    def test_blocked_when_capability_error(self):
        r = evaluate_arming(_settings(), "error")
        self.assertFalse(r.ready_to_arm)

    def test_blocked_when_max_notional_zero(self):
        r = evaluate_arming(_settings(max_notional_per_instrument=0), "trade")
        self.assertFalse(r.ready_to_arm)
        self.assertTrue(any("max_notional" in b for b in r.blockers))

    def test_blocked_when_max_daily_loss_zero(self):
        r = evaluate_arming(_settings(max_daily_loss_usdt=0), "trade")
        self.assertFalse(r.ready_to_arm)
        self.assertTrue(any("max_daily_loss" in b for b in r.blockers))

    def test_blocked_when_env_invalid(self):
        r = evaluate_arming(_settings(okx_environment="staging"), "trade")
        self.assertFalse(r.ready_to_arm)
        self.assertTrue(any("okx_environment" in b for b in r.blockers))


class TestEvaluateArmingWarnings(unittest.TestCase):
    def test_tiny_equity_warning_optional(self):
        r = evaluate_arming(_settings(), "trade", equity_usdt=12.5)
        self.assertTrue(r.ready_to_arm)
        self.assertTrue(any("tiny equity" in w for w in r.warnings))

    def test_no_equity_warning_when_omitted(self):
        r = evaluate_arming(_settings(), "trade")
        self.assertFalse(any("tiny equity" in w for w in r.warnings))

    def test_market_source_synthetic_warning(self):
        r = evaluate_arming(_settings(), "trade", market_source="synthetic")
        self.assertTrue(r.ready_to_arm)
        self.assertTrue(any("market_source" in w for w in r.warnings))

    def test_okx_public_no_market_warning(self):
        r = evaluate_arming(_settings(), "trade", market_source="okx_public")
        self.assertFalse(any("market_source" in w for w in r.warnings))

    def test_worker_stale_warning(self):
        r = evaluate_arming(_settings(), "trade", worker_stale=True)
        self.assertTrue(any("worker_stale" in w for w in r.warnings))

    def test_kill_switch_already_off_warning(self):
        r = evaluate_arming(_settings(kill_switch=False), "trade")
        self.assertTrue(r.ready_to_arm)
        self.assertFalse(r.kill_switch)
        self.assertTrue(any("already OFF" in w for w in r.warnings))


if __name__ == "__main__":
    unittest.main()
