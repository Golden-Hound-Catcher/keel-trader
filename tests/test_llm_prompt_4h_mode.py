"""LLM prompt 4h wording follows the effective KEEL_LLM_4H_MODE (2026-09-29).

Hard (llm_demo default): prompt must tell the model 4h must match the side and
4h-neutral → WAIT. Soft override: byte-identical to the pre-change soft text.
The kernel veto (edge_overlay._htf_ok) stays as the backstop.
"""
from __future__ import annotations

import os
import subprocess
import unittest
from pathlib import Path
from unittest.mock import MagicMock

from keel.config import settings as settings_mod
from keel.domain.decision import Decision
from keel.factors.market_data import MarketSnapshot
from keel.llm.client import LLMResponse
from keel.llm.prompts import PromptComposer, htf_4h_prompt_variables
from keel.policy import LLMDecisionPolicy, PolicyContext
from keel.policy.edge_overlay import HTF_GATE, apply_llm_edge_overlay, effective_llm_4h_mode

_KEYS = (
    "KEEL_SKIP_DOTENV",
    "KEEL_PROFILE",
    "KEEL_LLM_4H_MODE",
    "KEEL_LLM_REQUIRE_4H",
    "KEEL_LLM_REQUIRE_1H",
)

_HARD_MARKERS = (
    "当前 trend4h 为 hard 模式",
    "多头需 trend4h=bullish，空头需 trend4h=bearish",
    "trend4h=neutral 或 4h 反向时一律直接输出 WAIT",
)
_SOFT_RULE = "trend4h 默认 soft（neutral 可开火，只要 1h 对齐；4h 反向必须 WAIT）"
_SOFT_15M = "soft-4h 且 4h=neutral 时：必须有 15m 同向确认"
_VARS = {"strategy_version": "t", "market_block": "- BTC-USDT-SWAP: x", "active_instruments": "BTC-USDT-SWAP"}


def _reset() -> None:
    settings_mod._DOTENV_LOADED = False
    settings_mod._DOTENV_VALUES.clear()
    settings_mod.get_settings.cache_clear()


class _EnvCase(unittest.TestCase):
    def setUp(self) -> None:
        self._saved = {k: os.environ.get(k) for k in _KEYS}
        for k in _KEYS:
            os.environ.pop(k, None)
        os.environ["KEEL_SKIP_DOTENV"] = "1"
        _reset()

    def tearDown(self) -> None:
        for k, v in self._saved.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v
        _reset()


class TestPrompt4hWording(_EnvCase):
    def test_hard_prompt_requires_4h_alignment_and_neutral_wait(self) -> None:
        a = PromptComposer().compose(variables=dict(_VARS, **htf_4h_prompt_variables("hard")))
        self.assertTrue(a.ok, msg=a.errors)
        self.assertEqual(a.warnings, [])
        for m in _HARD_MARKERS:
            self.assertIn(m, a.system)
        self.assertNotIn("默认 soft", a.system)
        self.assertNotIn("neutral 可开火", a.system)
        self.assertNotIn(_SOFT_15M, a.system)
        self.assertIn("4h 未与方向同向（hard-4h：4h=neutral 或反向均 WAIT）、15m 反向", a.user)
        self.assertNotIn("soft-4h", a.user)
        self.assertNotIn("{{", a.system + a.user)

    def test_soft_prompt_keeps_old_soft_wording(self) -> None:
        a = PromptComposer().compose(variables=dict(_VARS, **htf_4h_prompt_variables("soft")))
        self.assertTrue(a.ok, msg=a.errors)
        self.assertIn(_SOFT_RULE, a.system)
        self.assertIn(_SOFT_15M, a.system)
        self.assertIn("4h 反向、soft-4h 且 4h=neutral 无 15m 同向确认、15m 反向", a.user)
        for m in _HARD_MARKERS:
            self.assertNotIn(m, a.system)

    def test_soft_prompt_byte_identical_to_pre_change_modules(self) -> None:
        root = Path(__file__).resolve().parents[1]
        try:
            old = {
                m: subprocess.check_output(
                    ["git", "show", f"abac2a5:keel/llm/prompts/modules/{m}.txt"],
                    cwd=root,
                    stderr=subprocess.DEVNULL,
                ).decode("utf-8")
                for m in ("system_rules.v2", "user_task.v2")
            }
        except (OSError, subprocess.CalledProcessError):
            self.skipTest("git history unavailable")
        c = PromptComposer()
        c.register_inline("system_rules.old", old["system_rules.v2"])
        c.register_inline("user_task.old", old["user_task.v2"])
        new = c.compose(variables=dict(_VARS, htf_4h_mode="soft"))
        ref = c.compose(
            variables=_VARS,
            system_pipeline=["system_role.v1", "system_rules.old", "system_output.v1"],
            user_pipeline=["user_header.v1", "user_market.v1", "user_task.old"],
        )
        self.assertEqual(new.system, ref.system)
        self.assertEqual(new.user, ref.user)

    def test_off_prompt_drops_4h_requirement(self) -> None:
        a = PromptComposer().compose(variables=dict(_VARS, **htf_4h_prompt_variables("off")))
        self.assertTrue(a.ok, msg=a.errors)
        self.assertIn("当前不强制 trend4h", a.system)
        self.assertIn("1h 与方向不一致、15m 反向", a.user)

    def test_compose_without_vars_renders_effective_mode(self) -> None:
        os.environ["KEEL_PROFILE"] = "llm_demo"
        _reset()
        self.assertEqual(effective_llm_4h_mode(), "hard")
        a = PromptComposer().compose(variables=_VARS)
        self.assertTrue(a.ok, msg=a.errors)
        self.assertIn(_HARD_MARKERS[0], a.system)

        os.environ["KEEL_LLM_4H_MODE"] = "soft"
        _reset()
        self.assertEqual(effective_llm_4h_mode(), "soft")
        a = PromptComposer().compose(variables=_VARS)
        self.assertIn(_SOFT_RULE, a.system)

        os.environ["KEEL_LLM_REQUIRE_4H"] = "0"
        _reset()
        self.assertEqual(effective_llm_4h_mode(), "off")
        self.assertIn("当前不强制 trend4h", PromptComposer().compose(variables=_VARS).system)

    def test_unknown_mode_falls_back_to_soft(self) -> None:
        self.assertEqual(htf_4h_prompt_variables("weird")["htf_4h_mode"], "soft")


class TestLLMPolicyPrompt4h(_EnvCase):
    def _run_policy(self) -> tuple[str, str, dict]:
        client = MagicMock()
        client.request_decisions.return_value = LLMResponse(
            success=True,
            decisions={"BTC-USDT-SWAP": Decision(inst_id="BTC-USDT-SWAP", action="WAIT", reason="m")},
            latency_ms=1,
            model="mock",
        )
        snap = MarketSnapshot(  # type: ignore[call-arg]
            inst_id="BTC-USDT-SWAP", name="BTC", timestamp=1.0, price=100.0, data_valid=True
        )
        res = LLMDecisionPolicy(client=client).decide(
            PolicyContext(snapshots={snap.inst_id: snap}, instrument_ids=[snap.inst_id], timestamp=1.0)
        )
        system, user = client.request_decisions.call_args[0][:2]
        return system, user, res.prompt_meta

    def test_llm_demo_policy_sends_hard_prompt(self) -> None:
        os.environ["KEEL_PROFILE"] = "llm_demo"
        _reset()
        system, user, meta = self._run_policy()
        self.assertEqual(meta.get("llm_4h_mode"), "hard")
        for m in _HARD_MARKERS:
            self.assertIn(m, system)
        self.assertIn("hard-4h", user)

    def test_soft_override_policy_sends_soft_prompt(self) -> None:
        os.environ["KEEL_PROFILE"] = "llm_demo"
        os.environ["KEEL_LLM_4H_MODE"] = "soft"
        _reset()
        system, user, meta = self._run_policy()
        self.assertEqual(meta.get("llm_4h_mode"), "soft")
        self.assertIn(_SOFT_RULE, system)
        self.assertIn(_SOFT_15M, system)

    def test_code_veto_backstop_still_fires_in_hard(self) -> None:
        """Prompt change does not remove the kernel veto / htf_veto_cause."""
        os.environ["KEEL_PROFILE"] = "llm_demo"
        _reset()
        snap = MarketSnapshot(  # type: ignore[call-arg]
            inst_id="SOL-USDT-SWAP", name="SOL", timestamp=1.0, price=117.85, atr_14=0.7,
            rsi_14=40.0, trend_15m="bearish", trend_1h="bearish", trend_4h="neutral", data_valid=True,
        )
        snap.adx_14 = 30.0  # type: ignore[attr-defined]
        dec = Decision(
            inst_id="SOL-USDT-SWAP", action="SELL_SHORT", confidence=75.0, entry_price=117.85,
            take_profit=114.6, stop_loss=119.2, leverage=3, margin_usdt=40.0, reason="x",
        )
        out = apply_llm_edge_overlay(dec, snap)
        self.assertEqual(out.action, "WAIT")
        diag = out.signal_diag or {}
        self.assertIn(HTF_GATE, diag.get("missing") or [])
        self.assertEqual(diag.get("htf_veto_cause"), "4h_hard_neutral")
        self.assertIn("4h hard veto", out.reason)


if __name__ == "__main__":
    unittest.main()
