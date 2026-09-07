"""Smoke: observe stack scripts exist, executable, and pass bash -n."""

from __future__ import annotations

import stat
import subprocess
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SCRIPTS = (
    "observe_up.sh",
    "observe_down.sh",
    "observe_status.sh",
)


class ObserveScriptsTest(unittest.TestCase):
    def test_scripts_exist_executable_and_syntax(self) -> None:
        for name in SCRIPTS:
            path = ROOT / "scripts" / name
            self.assertTrue(path.is_file(), f"missing {path}")
            mode = path.stat().st_mode
            self.assertTrue(mode & stat.S_IXUSR, f"not executable: {path}")
            proc = subprocess.run(
                ["bash", "-n", str(path)],
                check=False,
                capture_output=True,
                text=True,
            )
            self.assertEqual(
                proc.returncode,
                0,
                f"bash -n failed for {name}: {proc.stderr}",
            )

    def test_observe_up_hardening_markers(self) -> None:
        """Lightweight shellcheck-style asserts (no shellcheck binary required)."""
        text = (ROOT / "scripts" / "observe_up.sh").read_text(encoding="utf-8")
        self.assertIn("set -euo pipefail", text)
        self.assertIn("KEEL_OBSERVE_FORCE", text)
        self.assertIn("address already in use", text.lower())
        self.assertIn("_listener_pids", text)
        self.assertIn("_tail_log", text)
        self.assertIn("scheduler", text.lower())
        # FORCE only stops our pidfile PIDs — never unknown listeners
        self.assertIn("never unknown", text.lower())
        self.assertIn("foreign listener", text.lower())
        self.assertNotIn("fuser -k", text)

    def test_observe_status_mismatch_warn(self) -> None:
        text = (ROOT / "scripts" / "observe_status.sh").read_text(encoding="utf-8")
        self.assertIn("set -euo pipefail", text)
        self.assertIn("okx_configured", text)
        self.assertIn("STALE API", text)
        self.assertIn("KEEL_OKX_ENV", text)


if __name__ == "__main__":
    unittest.main()
