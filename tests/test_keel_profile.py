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
        self.assertEqual(d["KEEL_LLM_4H_MODE"], "hard")  # 2026-09-29 (was soft)
        self.assertEqual(d["KEEL_RULE_4H_MODE"], "hard")
        self.assertEqual(d["KEEL_LLM_JSON_OBJECT"], "0")
        self.assertEqual(d["KEEL_KILL_SWITCH"], "0")
        self.assertEqual(d["KEEL_LLM_MIN_CONFIDENCE"], "65")
        self.assertEqual(d["KEEL_LLM_RSI_CHASE_LONG_MAX"], "65")
        self.assertEqual(d["KEEL_LLM_RSI_CHASE_SHORT_MIN"], "35")
        self.assertEqual(d["KEEL_LLM_POST_EXIT_COOLDOWN_SECONDS"], "7200")
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
                "KEEL_LLM_4H_MODE",
                "KEEL_LLM_ADX_MIN",
                "KEEL_LLM_SOFT4H_BLOCK_15M_NEUTRAL",
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

    def test_llm_demo_4h_mode_defaults_hard(self) -> None:
        """2026-09-29: llm_demo profile → effective LLM 4h mode hard."""
        from keel.policy.edge_overlay import _4h_mode

        os.environ["KEEL_PROFILE"] = "llm_demo"
        _reset()
        settings_mod.get_settings()
        self.assertEqual(settings_mod._env("KEEL_LLM_4H_MODE"), "hard")
        self.assertEqual(_4h_mode(), "hard")

    def test_llm_demo_4h_mode_env_override_soft(self) -> None:
        from keel.policy.edge_overlay import _4h_mode

        os.environ["KEEL_PROFILE"] = "llm_demo"
        os.environ["KEEL_LLM_4H_MODE"] = "soft"
        _reset()
        settings_mod.get_settings()
        self.assertEqual(_4h_mode(), "soft")

    def test_no_profile_4h_mode_code_default_soft(self) -> None:
        from keel.policy.edge_overlay import _4h_mode

        _reset()
        settings_mod.get_settings()
        self.assertEqual(_4h_mode(), "soft")

    def test_llm_demo_4h_neutral_short_waits_with_clear_reason(self) -> None:
        """Profile default: 1h/15m bearish + 4h neutral short → WAIT (4h hard veto)."""
        from keel.domain.decision import Decision
        from keel.factors.market_data import MarketSnapshot
        from keel.policy.edge_overlay import HTF_GATE, apply_llm_edge_overlay

        os.environ["KEEL_PROFILE"] = "llm_demo"
        _reset()
        settings_mod.get_settings()
        snap = MarketSnapshot(  # type: ignore[call-arg]
            inst_id="SOL-USDT-SWAP",
            name="SOL",
            timestamp=1.0,
            price=117.85,
            atr_14=0.7,
            rsi_14=40.0,
            trend_15m="bearish",
            trend_1h="bearish",
            trend_4h="neutral",
            data_valid=True,
        )
        snap.adx_14 = 30.0  # type: ignore[attr-defined]
        dec = Decision(
            inst_id="SOL-USDT-SWAP",
            action="SELL_SHORT",
            confidence=75.0,
            entry_price=117.85,
            take_profit=114.6,
            stop_loss=119.2,
            leverage=3,
            margin_usdt=40.0,
            reason="1h/15m bearish, 4h neutral",
        )
        out = apply_llm_edge_overlay(dec, snap)
        self.assertEqual(out.action, "WAIT")
        diag = out.signal_diag or {}
        self.assertIn(HTF_GATE, diag.get("missing") or [])
        self.assertEqual(diag.get("llm_4h_mode"), "hard")
        self.assertEqual(diag.get("htf_veto_cause"), "4h_hard_neutral")
        self.assertIn("4h hard veto: short needs t4h=bearish (t4h=neutral", out.reason)

        # Env override back to soft → same setup fires (F10: 15m same-dir confirms).
        os.environ["KEEL_LLM_4H_MODE"] = "soft"
        _reset()
        settings_mod.get_settings()
        out2 = apply_llm_edge_overlay(dec, snap)
        self.assertEqual(out2.action, "SELL_SHORT")
        self.assertEqual((out2.signal_diag or {}).get("llm_4h_mode"), "soft")

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
