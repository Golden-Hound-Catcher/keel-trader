"""Q1/S1: read-only arming checklist + economic gates (no network / no kill writes)."""
from __future__ import annotations

import time
import unittest
from types import SimpleNamespace

from keel.risk.arming import (
    AVG_NET_RT_BELOW,
    FULL_GATE_WIN_RATE_BELOW,
    INSUFFICIENT_POST_E31_FULL_GATE_SAMPLE,
    INSUFFICIENT_SHADOW_MARKOUT_SAMPLE,
    PROBE_WIN_RATE_BELOW,
    build_first_live,
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
    def __init__(
        self,
        events=None,
        markout=None,
        quality=None,
        full_gate_markout=None,
        full_gate_markout_by_cohort=None,
    ):
        self._events = list(events or [])
        self._markout = markout
        self._quality = quality
        self._fg_markout = full_gate_markout
        self._fg_by_cohort = full_gate_markout_by_cohort or {}

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

    def get_quality_stats(self, hours=24.0):
        if self._quality is not None:
            return self._quality
        return {
            "full_gate_fires": {"count": 0, "by_cohort": {}},
        }

    def get_full_gate_markout(
        self,
        hours=24.0,
        horizons=None,
        *,
        apply_funding=False,
        settings=None,
        cohort=None,
    ):
        if cohort and cohort in self._fg_by_cohort:
            return self._fg_by_cohort[cohort]
        if self._fg_markout is not None:
            return self._fg_markout
        return {
            "count": 0,
            "cohort": cohort or "full_gate",
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

    def test_economic_by_instrument_diagnostic(self):
        mk = _markout(count=12, probe_count=8, sample_count=8, probe_wr=0.7, avg_net=2.0)
        mk["by_instrument"] = {
            "BTC-USDT-SWAP": {
                "count": 8,
                "probe_count": 5,
                "by_action": {"BUY_LONG": 8},
                "by_skip_reason": {"below_hurdle": 3},
                "markout_300s": {
                    "sample_count": 6,
                    "probe_sample_count": 4,
                    "avg_net_roundtrip_markout_bps": 3.0,
                    "win_rate_net_roundtrip": 0.66,
                    "probe_avg_net_roundtrip_markout_bps": 2.5,
                    "probe_win_rate_net_roundtrip": 0.75,
                },
            },
            "ETH-USDT-SWAP": {
                "count": 4,
                "probe_count": 3,
                "by_action": {"SELL_SHORT": 4},
                "by_skip_reason": {},
                "markout_300s": {
                    "sample_count": 2,
                    "probe_sample_count": 1,
                    "avg_net_roundtrip_markout_bps": -1.0,
                    "win_rate_net_roundtrip": 0.0,
                    "probe_avg_net_roundtrip_markout_bps": -1.0,
                    "probe_win_rate_net_roundtrip": 0.0,
                },
            },
        }
        blockers, summary = evaluate_economic_gates(
            _settings(arming_econ_enabled=True),
            markout_stats=mk,
        )
        self.assertEqual(blockers, [])
        self.assertTrue(summary["passed"])
        by_inst = summary.get("by_instrument") or {}
        self.assertIn("BTC-USDT-SWAP", by_inst)
        self.assertEqual(by_inst["BTC-USDT-SWAP"]["fill_count"], 8)
        self.assertEqual(by_inst["BTC-USDT-SWAP"]["probe_count"], 5)
        self.assertEqual(by_inst["BTC-USDT-SWAP"]["sample_count"], 6)
        self.assertAlmostEqual(
            by_inst["BTC-USDT-SWAP"]["avg_net_roundtrip_markout_bps"], 3.0
        )
        self.assertEqual(by_inst["ETH-USDT-SWAP"]["fill_count"], 4)
        # Aggregate gate still uses overall metrics, not per-inst.
        self.assertEqual(summary["fill_count"], 12)

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



class TestFirstLiveChecklist(unittest.TestCase):
    """S2: first-live Stage T gate aggregation (read-only)."""

    def test_allowed_now_false_when_kill_on(self):
        arming = evaluate_arming(
            _settings(kill_switch=True, arming_econ_enabled=True),
            "trade",
            markout_stats=_markout(),
        )
        self.assertTrue(arming.ready_to_arm)
        self.assertTrue(arming.economic["passed"])
        fl = build_first_live(
            _settings(kill_switch=True, shadow_mode=False),
            arming,
        )
        self.assertFalse(fl.allowed_now)
        self.assertTrue(fl.kill_switch)
        self.assertIn("wait_for_economic_pass", fl.human_steps)
        self.assertIn("clear_kill_manually", fl.human_steps)
        self.assertIn("re_enable_kill", fl.human_steps)
        self.assertEqual(
            fl.suggested_live_caps["live_max_notional_per_instrument"], 200.0
        )
        self.assertEqual(
            fl.suggested_live_caps["live_max_contracts_per_instrument"], 5
        )

    def test_allowed_now_false_when_economic_fails(self):
        arming = evaluate_arming(
            _settings(kill_switch=False, arming_econ_enabled=True),
            "trade",
            markout_stats=_markout(sample_count=1, probe_sample_count=1),
        )
        self.assertFalse(arming.ready_to_arm)
        self.assertFalse(arming.economic["passed"])
        fl = build_first_live(
            _settings(kill_switch=False, shadow_mode=False),
            arming,
        )
        self.assertFalse(fl.allowed_now)
        self.assertIn(INSUFFICIENT_SHADOW_MARKOUT_SAMPLE, fl.blockers)

    def test_allowed_now_true_when_ready_kill_off_shadow_off(self):
        arming = evaluate_arming(
            _settings(kill_switch=False, arming_econ_enabled=True),
            "trade",
            markout_stats=_markout(),
        )
        self.assertTrue(arming.ready_to_arm)
        fl = build_first_live(
            _settings(kill_switch=False, shadow_mode=False, shadow_near_probe=False),
            arming,
        )
        self.assertTrue(fl.allowed_now)
        self.assertFalse(fl.kill_switch)
        self.assertFalse(fl.shadow_mode)
        self.assertEqual(fl.capability, "trade")
        self.assertEqual(len(fl.human_steps), 5)

    def test_allowed_now_false_when_shadow_on(self):
        arming = evaluate_arming(
            _settings(kill_switch=False, arming_econ_enabled=True),
            "trade",
            markout_stats=_markout(),
        )
        fl = build_first_live(
            _settings(kill_switch=False, shadow_mode=True),
            arming,
            shadow_mode=True,
        )
        self.assertFalse(fl.allowed_now)
        self.assertTrue(fl.shadow_mode)

    def test_never_mutates_settings(self):
        s = _settings(kill_switch=True)
        arming = evaluate_arming(s, "trade", markout_stats=_markout())
        build_first_live(s, arming)
        self.assertTrue(s.kill_switch)



def _fg_markout(*, count=1, sample_count=5, wr=0.60, avg_net=1.5, horizon=300, cohort="post_e31"):
    return {
        "count": count,
        "cohort": cohort,
        "markout": {
            "horizons": [
                {
                    "horizon_seconds": horizon,
                    "sample_count": sample_count,
                    "win_rate_net_roundtrip": wr,
                    "avg_net_roundtrip_markout_bps": avg_net,
                    "frac_clear_net_rt_hurdle": 0.2,
                }
            ]
        },
    }


class TestF1PostE31ArmingEconomic(unittest.TestCase):
    def test_insufficient_post_e31_when_prefer_path_and_thin_sample(self):
        """54 pre spray + 0 post → never use pre 24%; insufficient_post_e31."""
        quality = {
            "full_gate_fires": {
                "count": 54,
                "by_cohort": {
                    "post_e31": {"count": 0, "by_action": {}, "by_instrument": {}},
                    "pre_e31": {"count": 54, "by_action": {"SELL_SHORT": 54}, "by_instrument": {}},
                },
            }
        }
        # Shadow markout would otherwise look ok — FG prefer path must win.
        mk = _markout(count=12, probe_count=8, sample_count=8, probe_wr=0.7, avg_net=2.0)
        led = _FakeLedger(
            [_FakeEvent(time.time() - 10)],
            markout=mk,
            quality=quality,
            full_gate_markout_by_cohort={
                "post_e31": _fg_markout(count=0, sample_count=0, wr=None, avg_net=None),
                "pre_e31": _fg_markout(
                    count=54, sample_count=50, wr=0.24, avg_net=-9.7, cohort="pre_e31"
                ),
            },
        )
        blockers, summary = evaluate_economic_gates(
            _settings(arming_econ_enabled=True),
            ledger=led,
            markout_stats=mk,
        )
        self.assertIn(INSUFFICIENT_POST_E31_FULL_GATE_SAMPLE, blockers)
        self.assertNotIn(FULL_GATE_WIN_RATE_BELOW, blockers)
        self.assertEqual(summary["full_gate_fires"], 54)
        self.assertEqual(summary["full_gate_fires_pre_e31"], 54)
        self.assertEqual(summary["full_gate_fires_post_e31"], 0)
        self.assertEqual(summary["full_gate_cohort_used"], "post_e31_insufficient")
        self.assertEqual(summary["economic_sample_source"], "insufficient_post_e31")
        self.assertFalse(summary["passed"])

    def test_post_e31_win_rate_blocker_not_pre(self):
        quality = {
            "full_gate_fires": {
                "count": 25,
                "by_cohort": {
                    "post_e31": {"count": 8, "by_action": {}, "by_instrument": {}},
                    "pre_e31": {"count": 17, "by_action": {}, "by_instrument": {}},
                },
            }
        }
        mk = _markout(count=12, probe_count=8, sample_count=8, probe_wr=0.7, avg_net=2.0)
        led = _FakeLedger(
            [_FakeEvent(time.time() - 10)],
            markout=mk,
            quality=quality,
            full_gate_markout_by_cohort={
                "post_e31": _fg_markout(count=8, sample_count=6, wr=0.33, avg_net=-2.0),
            },
        )
        blockers, summary = evaluate_economic_gates(
            _settings(arming_econ_enabled=True),
            ledger=led,
            markout_stats=mk,
        )
        self.assertIn(FULL_GATE_WIN_RATE_BELOW, blockers)
        self.assertNotIn(INSUFFICIENT_POST_E31_FULL_GATE_SAMPLE, blockers)
        self.assertEqual(summary["full_gate_cohort_used"], "post_e31")
        self.assertEqual(summary["economic_sample_source"], "full_gate_post_e31")
        self.assertAlmostEqual(summary["full_gate_win_rate_net_roundtrip"], 0.33)

    def test_post_e31_pass_when_metrics_ok(self):
        quality = {
            "full_gate_fires": {
                "count": 22,
                "by_cohort": {
                    "post_e31": {"count": 10, "by_action": {}, "by_instrument": {}},
                    "pre_e31": {"count": 12, "by_action": {}, "by_instrument": {}},
                },
            }
        }
        mk = _markout(count=2, probe_count=0, sample_count=2, probe_sample_count=0)
        led = _FakeLedger(
            [_FakeEvent(time.time() - 10)],
            markout=mk,
            quality=quality,
            full_gate_markout_by_cohort={
                "post_e31": _fg_markout(count=10, sample_count=8, wr=0.625, avg_net=3.0),
            },
        )
        blockers, summary = evaluate_economic_gates(
            _settings(arming_econ_enabled=True),
            ledger=led,
            markout_stats=mk,
        )
        self.assertEqual(blockers, [])
        self.assertTrue(summary["passed"])
        self.assertEqual(summary["full_gate_cohort_used"], "post_e31")


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
