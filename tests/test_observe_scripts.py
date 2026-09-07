"""Smoke: observe stack scripts exist, executable, and pass bash -n."""

from __future__ import annotations

import os
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


if __name__ == "__main__":
    unittest.main()
