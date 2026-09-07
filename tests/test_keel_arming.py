"""Q1/S1: read-only arming checklist + economic gates (no network / no kill writes)."""
from __future__ import annotations

import time
import unittest
from types import SimpleNamespace

from keel.risk.arming import (
    AVG_NET_RT_BELOW,
    INSUFFICIENT_SHADOW_MARKOUT_SAMPLE,
    PROBE_WIN_RATE_BELOW,
    evaluate_arming,
    evaluate_economic_gates,
)


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
        # Checklist unit path: econ off unless a test opts in.
        arming_econ_enabled=False,
        arming_econ_hours=24.0,
        arming_econ_min_fills=10,
        arming_econ_min_probe_fills=5,
        arming_econ_min_markout_sample=5,
        arming_econ_markout_horizon_seconds=300,
        arming_econ_min_probe_win_rate_net_rt=0.55,
        arming_econ_min_avg_net_rt_bps=0.0,
    )
    base.update(kwargs)
    return SimpleNamespace(**base)


class _FakeEvent:
    def __init__(self, timestamp: float, event_type: str = "shadow_fill"):
        self.timestamp = timestamp
        self.event_type = event_type


class _FakeLedger:
    def __init__(self, events=None, markout=None):
        self._events = list(events or [])
        self._markout = markout

    def get_events(self, event_type=None, inst_id=None, limit=100):
        out = self._events
        if event_type is not None:
            out = [e for e in out if e.event_type == event_type]
        return out[:limit]

    def get_shadow_markout(self, hours=24.0, horizons=None):
        if self._markout is not None:
            return self._markout
        return {
            "hours": hours,
            "count": 0,
            "probe_count": 0,
            "by_skip_reason": {},
            "markout": {"horizons": []},
        }


def _markout(
    *,
    count=10,
    probe_count=5,
    sample_count=5,
    probe_sample_count=5,
    probe_wr=0.60,
    overall_wr=0.60,
    avg_net=1.5,
    horizon=300,
    by_skip=None,
):
    return {
        "hours": 24,
        "count": count,
        "probe_count": probe_count,
        "by_skip_reason": dict(by_skip or {}),
        "markout": {
            "horizons": [
                {
                    "horizon_seconds": horizon,
                    "sample_count": sample_count,
                    "probe_sample_count": probe_sample_count,
                    "probe_win_rate_net_roundtrip": probe_wr,
                    "win_rate_net_roundtrip": overall_wr,
                    "avg_net_roundtrip_markout_bps": avg_net,
                }
            ]
        },
    }


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


class TestEconomicArmingGates(unittest.TestCase):
    def test_insufficient_sample_is_blocker_not_pass(self):
        led = _FakeLedger(
            [_FakeEvent(time.time() - 60)],
            markout=_markout(count=5, probe_count=4, sample_count=4, probe_sample_count=3),
        )
        r = evaluate_arming(
            _settings(arming_econ_enabled=True),
            "trade",
            ledger=led,
        )
        self.assertFalse(r.ready_to_arm)
        self.assertIn(INSUFFICIENT_SHADOW_MARKOUT_SAMPLE, r.blockers)
        self.assertIsNotNone(r.economic)
        self.assertFalse(r.economic["passed"])
        self.assertFalse(r.economic["fills_ok"] and r.economic["sample_ok"])

    def test_small_current_ledger_shape_blocks(self):
        """Mirrors live cohort: few fills, low net-RT — not ready."""
        mk = _markout(
            count=5,
            probe_count=4,
            sample_count=5,
            probe_sample_count=4,
            probe_wr=0.25,
            overall_wr=0.2,
            avg_net=-7.97,
            by_skip={"below_hurdle": 12, "max_missing": 1},
        )
        r = evaluate_arming(
            _settings(arming_econ_enabled=True),
            "trade",
            ledger=_FakeLedger([_FakeEvent(time.time() - 10)], markout=mk),
        )
        self.assertFalse(r.ready_to_arm)
        self.assertIn(INSUFFICIENT_SHADOW_MARKOUT_SAMPLE, r.blockers)
        self.assertTrue(any("below_hurdle" in w for w in r.warnings))

    def test_pass_when_sample_and_metrics_ok(self):
        mk = _markout(
            count=12,
            probe_count=8,
            sample_count=8,
            probe_sample_count=8,
            probe_wr=0.625,
            avg_net=2.0,
            by_skip={"below_hurdle": 20},
        )
        r = evaluate_arming(
            _settings(arming_econ_enabled=True),
            "trade",
            ledger=_FakeLedger([_FakeEvent(time.time() - 10)], markout=mk),
        )
        self.assertTrue(r.ready_to_arm)
        self.assertEqual(r.blockers, [])
        self.assertTrue(r.economic["passed"])
        self.assertTrue(any("below_hurdle" in w for w in r.warnings))

    def test_probe_win_rate_blocker(self):
        mk = _markout(probe_wr=0.40, overall_wr=0.40, avg_net=1.0)
        blockers, summary = evaluate_economic_gates(
            _settings(arming_econ_enabled=True),
            markout_stats=mk,
        )
        self.assertIn(PROBE_WIN_RATE_BELOW, blockers)
        self.assertNotIn(INSUFFICIENT_SHADOW_MARKOUT_SAMPLE, blockers)
        self.assertFalse(summary["passed"])

    def test_avg_net_rt_blocker(self):
        mk = _markout(probe_wr=0.70, avg_net=-1.5)
        blockers, summary = evaluate_economic_gates(
            _settings(arming_econ_enabled=True),
            markout_stats=mk,
        )
        self.assertIn(AVG_NET_RT_BELOW, blockers)
        self.assertFalse(summary["passed"])

    def test_below_hurdle_skips_do_not_block_alone(self):
        mk = _markout(by_skip={"below_hurdle": 99, "cooldown": 1})
        blockers, summary = evaluate_economic_gates(
            _settings(arming_econ_enabled=True),
            markout_stats=mk,
        )
        self.assertEqual(blockers, [])
        self.assertTrue(summary["passed"])
        self.assertEqual(summary["by_skip_reason"].get("below_hurdle"), 99)

    def test_fills_or_probe_threshold(self):
        # count < 10 but probe_count >= 5 → fills_ok
        mk = _markout(count=6, probe_count=5, sample_count=5, probe_sample_count=5)
        blockers, summary = evaluate_economic_gates(
            _settings(arming_econ_enabled=True),
            markout_stats=mk,
        )
        self.assertTrue(summary["fills_ok"])
        self.assertEqual(blockers, [])

    def test_disabled_econ_skips(self):
        blockers, summary = evaluate_economic_gates(
            _settings(arming_econ_enabled=False),
            markout_stats=_markout(count=0, probe_count=0, sample_count=0),
        )
        self.assertEqual(blockers, [])
        self.assertTrue(summary["passed"])

    def test_injected_markout_stats_kwarg(self):
        r = evaluate_arming(
            _settings(arming_econ_enabled=True),
            "trade",
            markout_stats=_markout(),
        )
        self.assertTrue(r.ready_to_arm)
        self.assertTrue(r.economic["passed"])

    def test_no_markout_method_insufficient(self):
        class Bare:
            def get_events(self, **kwargs):
                return [_FakeEvent(time.time())]

        r = evaluate_arming(
            _settings(arming_econ_enabled=True),
            "trade",
            ledger=Bare(),
        )
        self.assertFalse(r.ready_to_arm)
        self.assertIn(INSUFFICIENT_SHADOW_MARKOUT_SAMPLE, r.blockers)


class TestArmingSettingsDefaults(unittest.TestCase):
    def test_settings_econ_defaults(self):
        from keel.config import refresh_settings
        import os

        prev = {k: os.environ.get(k) for k in list(os.environ) if k.startswith("KEEL_ARMING_ECON")}
        for k in list(os.environ):
            if k.startswith("KEEL_ARMING_ECON"):
                os.environ.pop(k, None)
        try:
            s = refresh_settings()
            self.assertTrue(s.arming_econ_enabled)
            self.assertEqual(s.arming_econ_min_fills, 10)
            self.assertEqual(s.arming_econ_min_probe_fills, 5)
            self.assertEqual(s.arming_econ_min_markout_sample, 5)
            self.assertEqual(s.arming_econ_markout_horizon_seconds, 300)
            self.assertAlmostEqual(s.arming_econ_min_probe_win_rate_net_rt, 0.55)
            self.assertAlmostEqual(s.arming_econ_min_avg_net_rt_bps, 0.0)
        finally:
            for k in list(os.environ):
                if k.startswith("KEEL_ARMING_ECON"):
                    os.environ.pop(k, None)
            for k, v in prev.items():
                if v is None:
                    os.environ.pop(k, None)
                else:
                    os.environ[k] = v
            refresh_settings()


if __name__ == "__main__":
    unittest.main()
