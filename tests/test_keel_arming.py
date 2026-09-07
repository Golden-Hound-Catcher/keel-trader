"""Q1: read-only arming checklist (mock capability; no network / no kill-switch writes)."""
from __future__ import annotations

import time
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
        live_max_notional_per_instrument=200.0,
        live_max_contracts_per_instrument=5,
        arming_shadow_hours=24.0,
        arming_require_shadow=False,
    )
    base.update(kwargs)
    return SimpleNamespace(**base)


class _FakeEvent:
    def __init__(self, timestamp: float, event_type: str = "shadow_fill"):
        self.timestamp = timestamp
        self.event_type = event_type


class _FakeLedger:
    def __init__(self, events=None):
        self._events = list(events or [])

    def get_events(self, event_type=None, inst_id=None, limit=100):
        out = self._events
        if event_type is not None:
            out = [e for e in out if e.event_type == event_type]
        return out[:limit]


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



class TestEvaluateArmingShadowRehearsal(unittest.TestCase):
    def test_warning_when_no_shadow_fill(self):
        led = _FakeLedger([])
        r = evaluate_arming(_settings(), "trade", ledger=led)
        self.assertTrue(r.ready_to_arm)
        self.assertTrue(any("no recent shadow_fill rehearsal" in w for w in r.warnings))
        self.assertFalse(any("shadow_fill" in b for b in r.blockers))

    def test_no_shadow_warning_when_recent_shadow_fill(self):
        led = _FakeLedger([_FakeEvent(time.time() - 60)])
        r = evaluate_arming(_settings(), "trade", ledger=led)
        self.assertTrue(r.ready_to_arm)
        self.assertFalse(any("shadow_fill" in w for w in r.warnings))

    def test_blocker_when_require_shadow_and_missing(self):
        led = _FakeLedger([])
        r = evaluate_arming(
            _settings(arming_require_shadow=True),
            "trade",
            ledger=led,
        )
        self.assertFalse(r.ready_to_arm)
        self.assertTrue(any("no recent shadow_fill rehearsal" in b for b in r.blockers))

    def test_stale_shadow_fill_counts_as_missing(self):
        led = _FakeLedger([_FakeEvent(time.time() - 48 * 3600)])
        r = evaluate_arming(_settings(arming_shadow_hours=24), "trade", ledger=led)
        self.assertTrue(any("no recent shadow_fill rehearsal" in w for w in r.warnings))

    def test_no_ledger_skips_shadow_check(self):
        r = evaluate_arming(_settings(), "trade")
        self.assertFalse(any("shadow_fill" in w for w in r.warnings))
        self.assertFalse(any("shadow_fill" in b for b in r.blockers))



if __name__ == "__main__":
    unittest.main()
