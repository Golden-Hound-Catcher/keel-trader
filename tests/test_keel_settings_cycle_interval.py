"""Settings: KEEL_CYCLE_INTERVAL_SECONDS parse/clamp + scheduler wiring."""
from __future__ import annotations

import os
import unittest

from keel.config import refresh_settings
from keel.config.settings import (
    CYCLE_INTERVAL_DEFAULT_SECONDS,
    CYCLE_INTERVAL_MAX_SECONDS,
    CYCLE_INTERVAL_MIN_SECONDS,
    clamp_cycle_interval_seconds,
)
from keel.worker.scheduler import KeelScheduler


class TestCycleIntervalSettings(unittest.TestCase):
    def tearDown(self):
        os.environ.pop("KEEL_CYCLE_INTERVAL_SECONDS", None)
        os.environ.pop("KEEL_ENABLE_LEGACY_SCHEDULER_JOBS", None)
        refresh_settings()

    def test_clamp_bounds(self):
        self.assertEqual(clamp_cycle_interval_seconds(30), CYCLE_INTERVAL_MIN_SECONDS)
        self.assertEqual(clamp_cycle_interval_seconds(60), 60)
        self.assertEqual(clamp_cycle_interval_seconds(900), 900)
        self.assertEqual(clamp_cycle_interval_seconds(86400), CYCLE_INTERVAL_MAX_SECONDS)
        self.assertEqual(clamp_cycle_interval_seconds(100_000), CYCLE_INTERVAL_MAX_SECONDS)

    def test_default_when_unset(self):
        os.environ.pop("KEEL_CYCLE_INTERVAL_SECONDS", None)
        s = refresh_settings()
        self.assertEqual(s.cycle_interval_seconds, CYCLE_INTERVAL_DEFAULT_SECONDS)

    def test_parse_valid_env(self):
        os.environ["KEEL_CYCLE_INTERVAL_SECONDS"] = "120"
        s = refresh_settings()
        self.assertEqual(s.cycle_interval_seconds, 120)

    def test_clamp_env_below_min(self):
        os.environ["KEEL_CYCLE_INTERVAL_SECONDS"] = "10"
        s = refresh_settings()
        self.assertEqual(s.cycle_interval_seconds, CYCLE_INTERVAL_MIN_SECONDS)

    def test_clamp_env_above_max(self):
        os.environ["KEEL_CYCLE_INTERVAL_SECONDS"] = "999999"
        s = refresh_settings()
        self.assertEqual(s.cycle_interval_seconds, CYCLE_INTERVAL_MAX_SECONDS)

    def test_invalid_env_falls_back_to_default_then_clamp(self):
        os.environ["KEEL_CYCLE_INTERVAL_SECONDS"] = "not-an-int"
        s = refresh_settings()
        self.assertEqual(s.cycle_interval_seconds, CYCLE_INTERVAL_DEFAULT_SECONDS)

    def test_scheduler_trader_job_uses_settings_interval(self):
        os.environ["KEEL_CYCLE_INTERVAL_SECONDS"] = "600"
        refresh_settings()
        sched = KeelScheduler()
        trader = sched._jobs["trader"]
        self.assertEqual(trader.interval_seconds, 600)
        # timeout = max(840, interval - 60) = max(840, 540) = 840
        self.assertEqual(trader.timeout_seconds, 840)

    def test_scheduler_timeout_scales_for_long_interval(self):
        os.environ["KEEL_CYCLE_INTERVAL_SECONDS"] = "3600"
        refresh_settings()
        sched = KeelScheduler()
        trader = sched._jobs["trader"]
        self.assertEqual(trader.interval_seconds, 3600)
        self.assertEqual(trader.timeout_seconds, 3540)  # max(840, 3600 - 60)

    def test_default_scheduler_jobs_trader_only(self):
        os.environ.pop("KEEL_ENABLE_LEGACY_SCHEDULER_JOBS", None)
        s = refresh_settings()
        self.assertFalse(s.enable_legacy_scheduler_jobs)
        self.assertEqual(s.scheduler_jobs, ("trader",))
        sched = KeelScheduler()
        self.assertEqual(list(sched._jobs.keys()), ["trader"])

    def test_legacy_scheduler_jobs_flag_restores_extras(self):
        os.environ["KEEL_ENABLE_LEGACY_SCHEDULER_JOBS"] = "1"
        s = refresh_settings()
        self.assertTrue(s.enable_legacy_scheduler_jobs)
        expected = (
            "trader",
            "factor_library",
            "news",
            "daily_briefing",
            "nightly_backup",
        )
        self.assertEqual(s.scheduler_jobs, expected)
        sched = KeelScheduler()
        self.assertEqual(list(sched._jobs.keys()), list(expected))

    def test_legacy_scheduler_jobs_true_string(self):
        os.environ["KEEL_ENABLE_LEGACY_SCHEDULER_JOBS"] = "true"
        s = refresh_settings()
        self.assertTrue(s.enable_legacy_scheduler_jobs)
        self.assertIn("factor_library", s.scheduler_jobs)


if __name__ == "__main__":
    unittest.main()
