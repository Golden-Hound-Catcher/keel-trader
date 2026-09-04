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



class TestLegacyBackendSoftBlock(unittest.TestCase):
    def test_require_legacy_backend_exits_without_flag(self):
        from keel.legacy import require_legacy_backend

        with patch.dict(os.environ, {"KEEL_ALLOW_LEGACY_BACKEND": ""}, clear=False):
            os.environ.pop("KEEL_ALLOW_LEGACY_BACKEND", None)
            # Simulate non-pytest import path for the guard itself.
            with patch("keel.legacy.legacy_backend_allowed", return_value=False):
                with self.assertRaises(SystemExit) as ctx:
                    require_legacy_backend(component="r20_backend.app")
                self.assertEqual(ctx.exception.code, 2)

    def test_require_legacy_backend_allows_with_flag(self):
        from keel.legacy import require_legacy_backend

        with patch.dict(os.environ, {"KEEL_ALLOW_LEGACY_BACKEND": "1"}):
            require_legacy_backend(component="r20_backend.app")  # no raise

    def test_uvicorn_style_import_exits_without_allow(self):
        """Accidental ``python -c 'import r20_backend.app'`` without flag fails."""
        env = {**os.environ, "PYTHONWARNINGS": "ignore"}
        env.pop("KEEL_ALLOW_LEGACY_BACKEND", None)
        env.pop("PYTEST_CURRENT_TEST", None)
        # Ensure subprocess does not inherit a pytest module via sitecustomize; fresh interp.
        result = subprocess.run(
            [
                sys.executable,
                "-c",
                "import importlib; importlib.import_module('r20_backend.app')",
            ],
            cwd=ROOT,
            capture_output=True,
            text=True,
            timeout=30,
            env=env,
        )
        self.assertEqual(result.returncode, 2, msg=result.stderr)
        self.assertIn("DISABLED", result.stderr)
        self.assertIn("keel.api.app", result.stderr)

    def test_uvicorn_style_import_ok_with_allow(self):
        """With opt-in, soft-block must not fire (full app import may need extra deps)."""
        env = {
            **os.environ,
            "PYTHONWARNINGS": "ignore",
            "KEEL_ALLOW_LEGACY_BACKEND": "1",
        }
        code = (
            "import importlib\n"
            "try:\n"
            "    importlib.import_module('r20_backend.app')\n"
            "    print('imported')\n"
            "except SystemExit as e:\n"
            "    print('sysexit', e.code)\n"
            "    raise\n"
            "except Exception as e:\n"
            "    print('past-guard', type(e).__name__)\n"
        )
        result = subprocess.run(
            [sys.executable, "-c", code],
            cwd=ROOT,
            capture_output=True,
            text=True,
            timeout=60,
            env=env,
        )
        self.assertNotEqual(result.returncode, 2, msg=result.stderr + result.stdout)
        self.assertNotIn("DISABLED", result.stderr)
        self.assertTrue(
            "imported" in result.stdout or "past-guard" in result.stdout,
            msg=result.stderr + result.stdout,
        )



class TestAdminApiRemoved(unittest.TestCase):
    def test_admin_auth_module_gone(self):
        self.assertFalse((ROOT / "r20_backend" / "admin_auth.py").exists())

    def test_dead_helper_modules_gone(self):
        for name in ("okx_client.py", "prompt_views.py", "account_baseline.py", "okx_trade_service.py"):
            self.assertFalse((ROOT / "r20_backend" / name).exists(), msg=name)

    def test_stub_returns_410_for_former_admin_paths(self):
        from fastapi.testclient import TestClient
        import r20_backend.app as app_module

        client = TestClient(app_module.app)
        for path in (
            "/api/v1/admin/overview",
            "/api/v1/admin/auth/login",
            "/admin",
            "/health",
        ):
            response = client.get(path)
            self.assertEqual(response.status_code, 410, msg=path)
            body = response.json()
            self.assertEqual(body.get("status"), "gone")
            self.assertIn("keel.api", body.get("prefer", ""))




class TestDeletedLegacyScripts(unittest.TestCase):
    def test_dashboard_era_scripts_removed(self):
        gone = [
            "sync_web_data.py",
            "daemon_web_sync.py",
            "generate_snapshots.py",
            "debug_aggregate_orders.py",
            "debug_audit_bills.py",
            "remove_retired_personal_wechat.py",
            "cleanup_disk.py",
            "calculus_replay.py",
            "calculus_engine.py",
        ]
        for name in gone:
            self.assertFalse((ROOT / "scripts" / name).exists(), msg=name)

    def test_dead_gateway_helpers_gone(self):
        for name in ("agents.py", "supervisor.py"):
            self.assertFalse((ROOT / "r20_gateway" / name).exists(), msg=name)

    def test_trader_shims_remain(self):
        self.assertTrue((ROOT / "scripts" / "ai_factor_trader.py").exists())
        self.assertTrue((ROOT / "scripts" / "ai_brain_trader.py").exists())

    def test_gateway_scheduler_tick_gated(self):
        from r20_gateway.scheduler import legacy_gateway_jobs_enabled

        with patch.dict(os.environ, {"KEEL_ENABLE_LEGACY_GATEWAY_SCHEDULER": ""}, clear=False):
            os.environ.pop("KEEL_ENABLE_LEGACY_GATEWAY_SCHEDULER", None)
            self.assertFalse(legacy_gateway_jobs_enabled())
        with patch.dict(os.environ, {"KEEL_ENABLE_LEGACY_GATEWAY_SCHEDULER": "1"}):
            self.assertTrue(legacy_gateway_jobs_enabled())


if __name__ == "__main__":
    unittest.main()
