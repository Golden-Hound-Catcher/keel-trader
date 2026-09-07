"""Repo-root .env feeds get_settings via in-memory map (os.environ wins)."""
from __future__ import annotations

import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from keel.config import settings as settings_mod


class TestDotenvLoad(unittest.TestCase):
    def tearDown(self) -> None:
        settings_mod._DOTENV_LOADED = False
        settings_mod._DOTENV_VALUES.clear()
        settings_mod.get_settings.cache_clear()
        os.environ["KEEL_SKIP_DOTENV"] = "1"
        for k in ("KEEL_OKX_ENV", "KEEL_KILL_SWITCH", "KEEL_MAX_POSITIONS"):
            os.environ.pop(k, None)

    def test_loads_dotenv_map_without_writing_os_environ(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / ".env").write_text(
                "KEEL_OKX_ENV=live\nKEEL_KILL_SWITCH=1\nKEEL_MAX_POSITIONS=3\n",
                encoding="utf-8",
            )
            fake_file = root / "keel" / "config" / "settings.py"
            fake_file.parent.mkdir(parents=True)
            fake_file.write_text("# stub\n", encoding="utf-8")

            for k in ("KEEL_OKX_ENV", "KEEL_KILL_SWITCH", "KEEL_MAX_POSITIONS"):
                os.environ.pop(k, None)
            os.environ.pop("KEEL_SKIP_DOTENV", None)

            with mock.patch.object(settings_mod, "__file__", str(fake_file)):
                settings_mod._DOTENV_LOADED = False
                settings_mod._DOTENV_VALUES.clear()
                settings_mod._load_dotenv_files()

            self.assertEqual(settings_mod._DOTENV_VALUES.get("KEEL_OKX_ENV"), "live")
            self.assertNotIn("KEEL_OKX_ENV", os.environ)
            self.assertEqual(settings_mod._env("KEEL_OKX_ENV"), "live")

            os.environ["KEEL_OKX_ENV"] = "demo"
            self.assertEqual(settings_mod._env("KEEL_OKX_ENV"), "demo")

    def test_skip_dotenv_flag(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / ".env").write_text("KEEL_OKX_ENV=live\n", encoding="utf-8")
            fake_file = root / "keel" / "config" / "settings.py"
            fake_file.parent.mkdir(parents=True)
            fake_file.write_text("# stub\n", encoding="utf-8")
            os.environ["KEEL_SKIP_DOTENV"] = "1"
            os.environ.pop("KEEL_OKX_ENV", None)
            with mock.patch.object(settings_mod, "__file__", str(fake_file)):
                settings_mod._DOTENV_LOADED = False
                settings_mod._DOTENV_VALUES.clear()
                settings_mod._load_dotenv_files()
            self.assertEqual(settings_mod._DOTENV_VALUES, {})
