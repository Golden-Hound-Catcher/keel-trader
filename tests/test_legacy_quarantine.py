"""Stage 7: legacy quarantine guards."""
from __future__ import annotations

import os
import subprocess
import sys
import tempfile
import unittest
import warnings
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]


class TestLegacySchedulerExits(unittest.TestCase):
    def test_r20_backend_scheduler_exits_nonzero(self):
        result = subprocess.run(
            [sys.executable, "-m", "r20_backend.scheduler"],
            cwd=ROOT,
            capture_output=True,
            text=True,
            timeout=15,
            env={**os.environ, "PYTHONWARNINGS": "ignore"},
        )
        self.assertEqual(result.returncode, 2)
        self.assertIn("DISABLED", result.stderr)

    def test_scheduler_main_raises_systemexit_2(self):
        from r20_backend.scheduler import main

        with self.assertRaises(SystemExit) as ctx:
            main()
        self.assertEqual(ctx.exception.code, 2)


class TestGatewayWorkerNoSchedulerByDefault(unittest.TestCase):
    def test_worker_skips_gateway_scheduler_without_flag(self):
        import r20_gateway.worker as worker

        with patch.dict(os.environ, {"KEEL_ENABLE_LEGACY_GATEWAY_SCHEDULER": ""}, clear=False):
            os.environ.pop("KEEL_ENABLE_LEGACY_GATEWAY_SCHEDULER", None)
            with tempfile.TemporaryDirectory() as tmp:
                lock = Path(tmp) / ".r20_gateway.lock"
                log = Path(tmp) / "r20_gateway.log"
                with (
                    patch.object(worker, "LOCK_FILE", lock),
                    patch.object(worker, "LOG_FILE", log),
                    patch("r20_gateway.worker.GatewayStore") as store_cls,
                    patch("r20_gateway.worker.GatewayScheduler") as sched_cls,
                ):
                    store_cls.return_value.recover_processing = lambda: None
                    prev = worker.RUNNING
                    try:
                        worker.RUNNING = False  # skip delivery loop after setup
                        worker.run()
                        sched_cls.assert_not_called()
                        self.assertTrue(log.exists())
                        self.assertIn("notification delivery only", log.read_text())
                    finally:
                        worker.RUNNING = prev


class TestLegacyWarnHelper(unittest.TestCase):
    def test_warn_legacy_respects_opt_in(self):
        import keel.legacy as legacy

        legacy._EMITTED.clear()
        with patch.dict(os.environ, {"KEEL_USE_LEGACY": "1"}):
            with warnings.catch_warnings(record=True) as caught:
                warnings.simplefilter("always")
                legacy.warn_legacy("unit-test-opt-in", prefer="keel.worker", loud=False)
            self.assertEqual(caught, [])

    def test_warn_legacy_emits_without_opt_in(self):
        import keel.legacy as legacy

        legacy._EMITTED.clear()
        with patch.dict(os.environ, {"KEEL_USE_LEGACY": ""}, clear=False):
            os.environ.pop("KEEL_USE_LEGACY", None)
            with warnings.catch_warnings(record=True) as caught:
                warnings.simplefilter("always")
                legacy.warn_legacy("unit-test-component", prefer="keel.api", loud=False)
            self.assertTrue(any(issubclass(w.category, DeprecationWarning) for w in caught))
            self.assertIn("unit-test-component", str(caught[0].message))


if __name__ == "__main__":
    unittest.main()
