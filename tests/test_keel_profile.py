"""KEEL_PROFILE applies defaults; explicit env wins."""
from __future__ import annotations

import os
import unittest

from keel.config import settings as settings_mod
from keel.config.profiles import LLM_DEMO, normalize_profile_name, profile_defaults
from keel.policy.stub import resolve_rule_variant


def _reset() -> None:
    settings_mod._DOTENV_LOADED = False
    settings_mod._DOTENV_VALUES.clear()
    settings_mod.get_settings.cache_clear()


class TestProfileHelpers(unittest.TestCase):
    def test_normalize_aliases(self) -> None:
        self.assertEqual(normalize_profile_name("llm_demo"), "llm_demo")
        self.assertEqual(normalize_profile_name("LLM-DEMO"), "llm_demo")
        self.assertEqual(normalize_profile_name("demo_llm"), "llm_demo")
        self.assertIsNone(normalize_profile_name(""))
        self.assertIsNone(normalize_profile_name("unknown"))

    def test_llm_demo_has_expected_keys(self) -> None:
        d = profile_defaults("llm_demo")
        self.assertEqual(d["KEEL_DECISION_POLICY"], "llm")
        self.assertEqual(d["KEEL_RULE_VARIANT"], "trend_follow")
        self.assertEqual(d["KEEL_LLM_4H_MODE"], "soft")
        self.assertEqual(d["KEEL_RULE_4H_MODE"], "hard")
        self.assertEqual(d["KEEL_LLM_JSON_OBJECT"], "0")
        self.assertEqual(d["KEEL_KILL_SWITCH"], "0")
        self.assertEqual(d["KEEL_LLM_MIN_CONFIDENCE"], "65")
        self.assertEqual(d["KEEL_LLM_RSI_CHASE_LONG_MAX"], "65")
        self.assertEqual(d["KEEL_LLM_RSI_CHASE_SHORT_MIN"], "35")
        self.assertIn("BTC-USDT-SWAP", d["KEEL_INSTRUMENTS"])


class TestProfileAppliesDefaults(unittest.TestCase):
    def setUp(self) -> None:
        self._saved = {
            k: os.environ.get(k)
            for k in (
                "KEEL_SKIP_DOTENV",
                "KEEL_PROFILE",
                "KEEL_DECISION_POLICY",
                "KEEL_LLM_MODEL",
                "KEEL_LLM_BASE_URL",
                "KEEL_LLM_JSON_OBJECT",
                "KEEL_RULE_VARIANT",
                "KEEL_INSTRUMENTS",
                "KEEL_KILL_SWITCH",
                "KEEL_SHADOW_MODE",
            )
        }
        # Isolate from developer .env file; profile still applies from process env.
        os.environ["KEEL_SKIP_DOTENV"] = "1"
        for k in list(self._saved):
            if k == "KEEL_SKIP_DOTENV":
                continue
            os.environ.pop(k, None)
        _reset()

    def tearDown(self) -> None:
        for k, v in self._saved.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v
        _reset()
        os.environ["KEEL_SKIP_DOTENV"] = "1"

    def test_profile_fills_unset_keys(self) -> None:
        os.environ["KEEL_PROFILE"] = "llm_demo"
        _reset()
        s = settings_mod.get_settings()
        self.assertEqual(s.profile, "llm_demo")
        self.assertEqual(s.llm_model, LLM_DEMO["KEEL_LLM_MODEL"])
        self.assertEqual(s.llm_base_url, LLM_DEMO["KEEL_LLM_BASE_URL"])
        self.assertFalse(s.llm_json_object)
        self.assertFalse(s.kill_switch)
        self.assertFalse(s.shadow_mode)
        self.assertEqual(
            s.instruments,
            ("BTC-USDT-SWAP", "ETH-USDT-SWAP", "SOL-USDT-SWAP"),
        )
        self.assertEqual(settings_mod._env("KEEL_DECISION_POLICY"), "llm")
        self.assertEqual(resolve_rule_variant(), "trend_follow")

    def test_explicit_env_wins_over_profile(self) -> None:
        os.environ["KEEL_PROFILE"] = "llm_demo"
        os.environ["KEEL_DECISION_POLICY"] = "rule"
        os.environ["KEEL_LLM_MODEL"] = "gpt-test"
        os.environ["KEEL_RULE_VARIANT"] = "mean_revert"
        os.environ["KEEL_KILL_SWITCH"] = "1"
        _reset()
        s = settings_mod.get_settings()
        self.assertEqual(s.profile, "llm_demo")
        self.assertEqual(s.llm_model, "gpt-test")
        self.assertTrue(s.kill_switch)
        self.assertEqual(settings_mod._env("KEEL_DECISION_POLICY"), "rule")
        self.assertEqual(resolve_rule_variant(), "mean_revert")

    def test_no_profile_keeps_builtin_defaults(self) -> None:
        _reset()
        s = settings_mod.get_settings()
        self.assertIsNone(s.profile)
        self.assertEqual(s.llm_model, "gpt-4o")
        self.assertTrue(s.llm_json_object)
        self.assertEqual(settings_mod._env("KEEL_DECISION_POLICY", "rule") or "rule", "rule")
        self.assertEqual(resolve_rule_variant(), "mean_revert")


if __name__ == "__main__":
    unittest.main()
