"""Legacy package absence + supported entrypoints (O3 hard-delete)."""
from __future__ import annotations

import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


class TestR20PackagesRemoved(unittest.TestCase):
    def test_r20_backend_package_absent(self):
        self.assertFalse((ROOT / "r20_backend").exists())

    def test_r20_gateway_package_absent(self):
        self.assertFalse((ROOT / "r20_gateway").exists())
        self.assertFalse((ROOT / "deploy" / "r20-gateway.service").exists())

    def test_legacy_systemd_units_removed(self):
        self.assertFalse((ROOT / "deploy" / "r20-quantum.service").exists())
        self.assertFalse((ROOT / "deploy" / "r20-scheduler.service").exists())

    def test_keel_legacy_module_removed(self):
        self.assertFalse((ROOT / "keel" / "legacy").exists())


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
            "qq_notifier.py",
            "r20_okx_setup.py",
            "ai_brain_trader.py",
            "db_manager.py",
            "factor_library.py",
            "news_sentiment_harvester.py",
            "daily_summary_and_backup.py",
            "nightly_backup_and_clean.py",
            "backup_runtime.py",
            "sync_full_ledger.py",
            "self_improvement_engine.py",
            "instrument_pool.py",
            "okx_runtime.py",
            "prompt_library.py",
        ]
        for name in gone:
            self.assertFalse((ROOT / "scripts" / name).exists(), msg=name)

    def test_retired_trader_scripts_gone(self):
        self.assertFalse((ROOT / "scripts" / "ai_factor_trader.py").exists())
        self.assertFalse((ROOT / "scripts" / "ai_brain_trader.py").exists())

    def test_qq_bind_test_removed(self):
        self.assertFalse((ROOT / "tests" / "test_qq_bind.py").exists())

    def test_council_manager_test_removed(self):
        self.assertFalse((ROOT / "tests" / "test_council_manager.py").exists())

    def test_okx_setup_test_removed(self):
        self.assertFalse((ROOT / "tests" / "test_okx_setup.py").exists())

    def test_scheduler_script_test_modules_removed(self):
        for name in (
            "test_prompt_library.py",
            "test_backup_methods.py",
            "test_prompt_math_foundations.py",
            "test_custom_systems.py",
            "test_gateway.py",
            "test_gateway_runtime.py",
            "test_gateway_scheduler.py",
            "test_notifications.py",
            "test_llm_multi_provider.py",
            "test_control_plane_v2.py",
            "test_open_source_control.py",
        ):
            self.assertFalse((ROOT / "tests" / name).exists(), msg=name)

    def test_ui_admin_artifacts_gone(self):
        self.assertFalse((ROOT / "dashboard").exists())
        self.assertFalse((ROOT / "frontend" / "src" / "views" / "admin").exists())


class TestSupportedEntrypoints(unittest.TestCase):
    def test_keel_api_app_importable(self):
        from keel.api.app import app

        self.assertIsNotNone(app)

    def test_keel_worker_cycle_entrypoint(self):
        env = os.environ.copy()
        with tempfile.TemporaryDirectory() as tmp:
            db = Path(tmp) / "cycle.db"
            result = subprocess.run(
                [sys.executable, "-m", "keel.worker.cycle", "--db", str(db)],
                cwd=ROOT,
                capture_output=True,
                text=True,
                timeout=30,
                env=env,
            )
            self.assertEqual(result.returncode, 0, msg=result.stderr + result.stdout)
            self.assertIn("Keel Trader", result.stdout)


if __name__ == "__main__":
    unittest.main()
