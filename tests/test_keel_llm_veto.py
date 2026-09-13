"""P4 LLM veto overlay: rule proposes, model may only WAIT or confirm."""
from __future__ import annotations

import unittest
from unittest.mock import MagicMock

from keel.domain.decision import Decision
from keel.factors.market_data import MarketSnapshot
from keel.llm.client import LLMResponse
from keel.llm.prompts.compose import (
    VETO_SYSTEM_PIPELINE,
    PromptComposer,
    format_rule_block,
)
from keel.policy import apply_llm_veto, build_decision_policy
from keel.policy.llm_policy import VetoLLMDecisionPolicy
from keel.policy.protocol import PolicyContext
from keel.policy.stub import RuleDecisionPolicy


def _long_rule(**kwargs) -> Decision:
    data = dict(
        inst_id="BTC-USDT-SWAP",
        action="BUY_LONG",
        confidence=70.0,
        entry_price=65000.0,
        take_profit=65880.0,
        stop_loss=64600.0,
        leverage=3,
        margin_usdt=50.0,
        reason="rule long score=4/4",
        signal_diag={"score": 4, "score_min": 4, "regime": "trend", "missing": []},
    )
    data.update(kwargs)
    return Decision(**data)  # type: ignore[arg-type]


class TestApplyLlmVeto(unittest.TestCase):
    def test_wait_cannot_be_upgraded(self):
        rule = Decision(inst_id="BTC-USDT-SWAP", action="WAIT", reason="no signal")
        llm = _long_rule(reason="I want to buy")
        out = apply_llm_veto(rule, llm)
        self.assertEqual(out.action, "WAIT")

    def test_same_side_confirms_keeps_geometry(self):
        rule = _long_rule()
        llm = Decision(
            inst_id=rule.inst_id,
            action="BUY_LONG",
            entry_price=1.0,
            take_profit=9.0,
            stop_loss=0.1,
            margin_usdt=20.0,
            reason="ok",
        )
        out = apply_llm_veto(rule, llm)
        self.assertEqual(out.action, "BUY_LONG")
        self.assertEqual(out.entry_price, 65000.0)
        self.assertEqual(out.take_profit, 65880.0)
        self.assertEqual(out.stop_loss, 64600.0)
        self.assertEqual(out.margin_usdt, 20.0)
        self.assertTrue(out.signal_diag["llm_confirm"])
        self.assertIn("llm confirm", out.reason)

    def test_cannot_increase_margin(self):
        rule = _long_rule(margin_usdt=50.0)
        llm = Decision(inst_id=rule.inst_id, action="BUY_LONG", margin_usdt=80.0)
        out = apply_llm_veto(rule, llm)
        self.assertEqual(out.margin_usdt, 50.0)

    def test_wait_vetoes(self):
        rule = _long_rule()
        llm = Decision(inst_id=rule.inst_id, action="WAIT", reason="narrative conflict")
        out = apply_llm_veto(rule, llm)
        self.assertEqual(out.action, "WAIT")
        self.assertTrue(out.signal_diag["llm_veto"])
        self.assertIn("narrative conflict", out.reason)
        self.assertEqual(out.signal_diag.get("nearest"), "none")
        self.assertIn("llm_veto_ok", out.signal_diag.get("missing") or [])

    def test_wait_vetoes_clears_near_signal(self):
        rule = _long_rule(
            signal_diag={"nearest": "long", "missing": [], "regime": "trend"}
        )
        llm = Decision(inst_id=rule.inst_id, action="WAIT", reason="chop")
        out = apply_llm_veto(rule, llm)
        self.assertEqual(out.action, "WAIT")
        self.assertEqual(out.signal_diag["nearest"], "none")
        self.assertEqual(out.signal_diag["nearest_before_suppress"], "long")
        self.assertIn("llm_veto_ok", out.signal_diag["missing"])
        from keel.execution.near_probe import near_signal_meets_gates

        self.assertFalse(near_signal_meets_gates(out.signal_diag, max_missing=2))

    def test_flip_discarded(self):
        rule = _long_rule()
        llm = Decision(inst_id=rule.inst_id, action="SELL_SHORT", reason="flip")
        out = apply_llm_veto(rule, llm)
        self.assertEqual(out.action, "WAIT")
        self.assertIn("flip", out.reason)
        self.assertEqual(out.signal_diag.get("nearest"), "none")
        self.assertIn("llm_veto_ok", out.signal_diag.get("missing") or [])

    def test_missing_llm_fail_closed(self):
        rule = _long_rule()
        out = apply_llm_veto(rule, None, fail_open=False)
        self.assertEqual(out.action, "WAIT")
        self.assertTrue(out.signal_diag["llm_unavailable"])

    def test_missing_llm_fail_open(self):
        rule = _long_rule()
        out = apply_llm_veto(rule, None, fail_open=True)
        self.assertEqual(out.action, "BUY_LONG")
        self.assertEqual(out.entry_price, 65000.0)


class TestVetoPolicy(unittest.TestCase):
    def test_no_candidates_skips_llm(self):
        inner = MagicMock(spec=RuleDecisionPolicy)
        inner.decide.return_value = type(
            "R",
            (),
            {
                "decisions": {
                    "BTC-USDT-SWAP": Decision(
                        inst_id="BTC-USDT-SWAP", action="WAIT", reason="flat"
                    )
                },
                "success": True,
            },
        )()
        client = MagicMock()
        policy = VetoLLMDecisionPolicy(client=client, inner=inner)
        snap = MarketSnapshot(
            inst_id="BTC-USDT-SWAP",
            name="BTC",
            timestamp=1.0,
            price=65000.0,
            atr_14=400.0,
            data_valid=True,
        )
        result = policy.decide(
            PolicyContext(
                snapshots={snap.inst_id: snap},
                instrument_ids=[snap.inst_id],
                timestamp=1.0,
            )
        )
        self.assertEqual(result.policy_name, "llm_veto")
        self.assertEqual(result.decisions[snap.inst_id].action, "WAIT")
        client.request_decisions.assert_not_called()
        self.assertFalse(result.prompt_meta["llm_called"])

    def test_veto_prompt_mentions_referee(self):
        composer = PromptComposer()
        assembled = composer.compose(
            variables={
                "strategy_version": "t",
                "active_instruments": "BTC-USDT-SWAP",
                "timestamp": "1",
                "market_block": "- BTC",
                "rule_block": "- BTC: RULE BUY_LONG",
            },
            system_pipeline=VETO_SYSTEM_PIPELINE,
            user_pipeline=("user_veto.v1",),
        )
        self.assertTrue(assembled.ok, msg=assembled.errors)
        self.assertIn("裁判", assembled.system)
        self.assertIn("不得把 WAIT 改成开仓", assembled.system)
        self.assertIn("RULE BUY_LONG", assembled.user)

    def test_format_rule_block_lists_score(self):
        text = format_rule_block({"BTC-USDT-SWAP": _long_rule()})
        self.assertIn("BUY_LONG", text)
        self.assertIn("score=4/4", text)
        self.assertIn("65000", text)

    def test_factory_llm_veto_without_key_falls_back_to_rule(self):
        settings = MagicMock()
        settings.llm_configured = False
        policy = build_decision_policy(settings=settings, name="llm_veto")
        self.assertEqual(policy.name, "rule")

    def test_factory_llm_veto_with_client(self):
        settings = MagicMock()
        settings.llm_configured = False
        client = MagicMock()
        policy = build_decision_policy(
            settings=settings, name="llm_veto", client=client
        )
        self.assertEqual(policy.name, "llm_veto")

    def test_overlay_vetoes_a_fire(self):
        inner = MagicMock(spec=RuleDecisionPolicy)
        fire = _long_rule()
        inner.decide.return_value = type("R", (), {"decisions": {fire.inst_id: fire}})()
        client = MagicMock()
        client.request_decisions.return_value = LLMResponse(
            success=True,
            decisions={
                fire.inst_id: Decision(
                    inst_id=fire.inst_id, action="WAIT", reason="chop"
                )
            },
        )
        policy = VetoLLMDecisionPolicy(client=client, inner=inner)
        snap = MarketSnapshot(
            inst_id=fire.inst_id,
            name="BTC",
            timestamp=1.0,
            price=65000.0,
            atr_14=400.0,
            data_valid=True,
        )
        result = policy.decide(
            PolicyContext(
                snapshots={snap.inst_id: snap},
                instrument_ids=[snap.inst_id],
                timestamp=1.0,
            )
        )
        self.assertEqual(result.decisions[fire.inst_id].action, "WAIT")
        self.assertTrue(result.decisions[fire.inst_id].signal_diag["llm_veto"])
        client.request_decisions.assert_called_once()
        args = client.request_decisions.call_args[0]
        self.assertIn("裁判", args[0])
        self.assertIn("RULE BUY_LONG", args[1])
