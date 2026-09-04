"""Gateway Scheduler timing and migration tests."""
from __future__ import annotations
from datetime import datetime, timedelta, timezone
from pathlib import Path
import os
import tempfile
import unittest
from unittest.mock import patch

from r20_gateway.scheduler import (
    GatewayScheduler,
    JOBS,
    JobSpec,
    backup_job_specs,
    legacy_gateway_jobs_enabled,
)
from r20_gateway.store import GatewayStore

BJ = timezone(timedelta(hours=8))


class GatewaySchedulerTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.store = GatewayStore(Path(self.temp.name) / "gateway.db")
        self.scheduler = GatewayScheduler(self.store, max_workers=1)
        self.now = datetime(2026, 9, 1, 18, 0, tzinfo=BJ)

    def tearDown(self):
        self.scheduler.shutdown()
        self.temp.cleanup()

    def test_legacy_script_jobs_retired(self):
        self.assertEqual(JOBS, ())
        self.assertEqual(backup_job_specs(), ())

    def test_migration_baseline_prevents_immediate_launch(self):
        self.scheduler.initialize_migration_baseline(self.now)
        with patch.dict(os.environ, {"KEEL_ENABLE_LEGACY_GATEWAY_SCHEDULER": "1"}):
            with patch("r20_gateway.scheduler.load_schedule", return_value={}):
                self.assertEqual(self.scheduler.tick(self.now), [])

    def test_interval_job_becomes_due_after_interval(self):
        fl = JobSpec("interval_demo", "gone.py", 60, 55)
        self.store.set_state(
            "job.last.interval_demo",
            (self.now - timedelta(seconds=61)).isoformat(),
        )
        self.assertTrue(self.scheduler.due(fl, self.now, {}))
        self.store.set_state("job.last.interval_demo", self.now.isoformat())
        self.assertFalse(self.scheduler.due(fl, self.now, {}))

    def test_daily_job_runs_once_per_time_slot(self):
        briefing = JobSpec(
            "daily_slot",
            "gone.py",
            None,
            600,
            "briefing_times",
            ("08:00", "20:00"),
        )
        schedule = {"briefing_times": ["08:00", "20:00"]}
        at_eight = self.now.replace(hour=8)
        self.assertTrue(self.scheduler.due(briefing, at_eight, schedule))
        self.store.set_state("job.last.daily_slot", at_eight.isoformat())
        self.assertFalse(self.scheduler.due(briefing, at_eight, schedule))
        self.assertTrue(self.scheduler.due(briefing, at_eight.replace(hour=20), schedule))

    def test_runtime_state_survives_store_reopen(self):
        self.store.set_state("job.last.news", self.now.isoformat())
        reopened = GatewayStore(self.store.path)
        self.assertEqual(reopened.get_state("job.last.news"), self.now.isoformat())

    def test_tick_is_noop_without_legacy_flag(self):
        """Defense-in-depth: due jobs must not launch when flag unset."""
        demo = JobSpec("interval_demo", "gone.py", 60, 55)
        self.store.set_state(
            "job.last.interval_demo",
            (self.now - timedelta(seconds=61)).isoformat(),
        )
        self.assertTrue(self.scheduler.due(demo, self.now, {}))
        with patch.dict(os.environ, {"KEEL_ENABLE_LEGACY_GATEWAY_SCHEDULER": ""}, clear=False):
            os.environ.pop("KEEL_ENABLE_LEGACY_GATEWAY_SCHEDULER", None)
            self.assertFalse(legacy_gateway_jobs_enabled())
            with patch("r20_gateway.scheduler.current_jobs", return_value=(demo,)):
                with patch("r20_gateway.scheduler.load_schedule", return_value={}):
                    with patch.object(self.scheduler.executor, "submit") as submit:
                        self.assertEqual(self.scheduler.tick(self.now), [])
                        submit.assert_not_called()

    def test_tick_may_launch_when_legacy_flag_set(self):
        demo = JobSpec("interval_demo", "gone.py", 60, 55)
        self.store.set_state(
            "job.last.interval_demo",
            (self.now - timedelta(seconds=61)).isoformat(),
        )
        with patch.dict(os.environ, {"KEEL_ENABLE_LEGACY_GATEWAY_SCHEDULER": "1"}):
            with patch("r20_gateway.scheduler.current_jobs", return_value=(demo,)):
                with patch("r20_gateway.scheduler.load_schedule", return_value={}):
                    with patch.object(self.scheduler.executor, "submit") as submit:
                        submit.return_value = type("F", (), {"done": lambda self: True})()
                        launched = self.scheduler.tick(self.now)
                        self.assertIn("interval_demo", launched)
                        submit.assert_called()


if __name__ == "__main__":
    unittest.main()
