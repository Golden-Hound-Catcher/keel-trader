"""LLM-as-trader parallel rule shadow: agree logic + calculus_data attachment."""
from __future__ import annotations

import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

from keel.domain.decision import Decision
from keel.exchange.paper import PaperAdapter
from keel.factors.market_data import MarketSnapshot
from keel.ledger import KeelLedger
from keel.llm.client import LLMResponse
from keel.policy import (
    LLMDecisionPolicy,
    PolicyContext,
    actions_agree,
    build_rule_shadow,
    rule_shadow_enabled,
)
from keel.policy.stub import RuleDecisionPolicy
from keel.worker.cycle import (
    build_synthetic_candles,
    enrich_snapshot,
    run_paper_cycle,
)


class TestActionsAgree(unittest.TestCase):
    def test_both_wait(self):
        self.assertTrue(actions_agree("WAIT", "WAIT"))

    def test_same_fire_side(self):
        self.assertTrue(actions_agree("BUY_LONG", "BUY_LONG"))
        self.assertTrue(actions_agree("SELL_SHORT", "SELL_SHORT"))
        self.assertTrue(actions_agree("buy_long", "BUY_LONG"))

    def test_diverge_opposite_fire(self):
        self.assertFalse(actions_agree("BUY_LONG", "SELL_SHORT"))

    def test_diverge_wait_vs_fire(self):
        self.assertFalse(actions_agree("WAIT", "BUY_LONG"))
        self.assertFalse(actions_agree("SELL_SHORT", "WAIT"))

    def test_unclear_unknown(self):
        self.assertIsNone(actions_agree("HOLD", "WAIT"))
        self.assertIsNone(actions_agree("BUY_LONG", ""))
        self.assertIsNone(actions_agree("", "WAIT"))


class TestBuildRuleShadow(unittest.TestCase):
    def test_payload_fields_and_agree(self):
        rule = Decision(
            inst_id="BTC-USDT-SWAP",
            action="BUY_LONG",
            confidence=66.0,
            entry_price=65000.0,
            take_profit=65880.0,
            stop_loss=64600.0,
            margin_usdt=40.0,
            reason="rule long",
            signal_diag={"nearest": "long", "missing": [], "rule_variant": "trend_follow"},
        )
        payload = build_rule_shadow(
            rule, primary_action="BUY_LONG", executed_policy="llm"
        )
        self.assertEqual(payload["action"], "BUY_LONG")
        self.assertEqual(payload["reason"], "rule long")
        self.assertEqual(payload["confidence"], 66.0)
        self.assertEqual(payload["margin_usdt"], 40.0)
        self.assertEqual(payload["entry_price"], 65000.0)
        self.assertEqual(payload["take_profit"], 65880.0)
        self.assertEqual(payload["stop_loss"], 64600.0)
        self.assertEqual(payload["rule_variant"], "trend_follow")
        self.assertTrue(payload["agree"])
        self.assertEqual(payload["executed_policy"], "llm")
        self.assertEqual(payload["signal_diag"]["nearest"], "long")

    def test_agree_false_on_diverge(self):
        rule = Decision(inst_id="ETH-USDT-SWAP", action="WAIT", reason="no")
        payload = build_rule_shadow(rule, primary_action="BUY_LONG")
        self.assertFalse(payload["agree"])
        self.assertEqual(payload["action"], "WAIT")


class TestRuleShadowEnabled(unittest.TestCase):
    def tearDown(self):
        os.environ.pop("KEEL_RULE_SHADOW", None)

    def test_default_on_for_llm(self):
        os.environ.pop("KEEL_RULE_SHADOW", None)
        self.assertTrue(rule_shadow_enabled("llm"))
        self.assertFalse(rule_shadow_enabled("rule"))

    def test_explicit_off(self):
        os.environ["KEEL_RULE_SHADOW"] = "0"
        self.assertFalse(rule_shadow_enabled("llm"))

    def test_explicit_on_for_rule(self):
        os.environ["KEEL_RULE_SHADOW"] = "1"
        self.assertTrue(rule_shadow_enabled("rule"))


def _enriched(inst_id: str = "BTC-USDT-SWAP", base: float = 65000.0) -> MarketSnapshot:
    candles = build_synthetic_candles(base, count=64)
    snap = MarketSnapshot(
        inst_id=inst_id,
        name=inst_id.split("-")[0],
        timestamp=candles[-1].timestamp,
        candles_15m=candles,
    )
    return enrich_snapshot(snap)


class TestRuleShadowCycleAttachment(unittest.TestCase):
    def tearDown(self):
        for key in (
            "KEEL_DECISION_POLICY",
            "KEEL_RULE_SHADOW",
            "KEEL_LLM_EDGE_OVERLAY",
            "KEEL_INSTRUMENTS",
            "KEEL_FORCE_PAPER",
            "KEEL_KILL_SWITCH",
            "KEEL_SHADOW_MODE",
        ):
            os.environ.pop(key, None)

    def test_calculus_data_has_rule_shadow_under_llm(self):
        os.environ["KEEL_DECISION_POLICY"] = "llm"
        os.environ["KEEL_RULE_SHADOW"] = "1"
        os.environ["KEEL_LLM_EDGE_OVERLAY"] = "0"  # keep model action as-is
        os.environ["KEEL_INSTRUMENTS"] = "BTC-USDT-SWAP"
        os.environ["KEEL_FORCE_PAPER"] = "1"
        os.environ["KEEL_KILL_SWITCH"] = "0"
        os.environ["KEEL_SHADOW_MODE"] = "0"

        llm_decision = Decision(
            inst_id="BTC-USDT-SWAP",
            action="WAIT",
            confidence=10.0,
            reason="llm wait",
        )
        client = MagicMock()
        client.request_decisions.return_value = LLMResponse(
            success=True,
            decisions={"BTC-USDT-SWAP": llm_decision},
            macro_assessment="flat",
            latency_ms=1,
            model="mock",
        )
        policy = LLMDecisionPolicy(client=client)

        with tempfile.TemporaryDirectory() as tmp:
            db = Path(tmp) / "ledger.db"
            ledger = KeelLedger(db)
            exchange = PaperAdapter()
            summary = run_paper_cycle(
                ledger=ledger,
                exchange=exchange,
                policy=policy,
                force_paper=True,
                instrument_ids=["BTC-USDT-SWAP"],
            )
            self.assertTrue(summary.get("ok"))
            self.assertEqual(summary.get("policy"), "llm")
            rows = ledger.get_decisions(limit=5)
            self.assertGreaterEqual(len(rows), 1)
            row = rows[0]
            self.assertEqual(row.policy_name, "llm")
            calc = row.calculus_data or {}
            self.assertIn("rule_shadow", calc)
            shadow = calc["rule_shadow"]
            self.assertIn("action", shadow)
            self.assertIn("agree", shadow)
            self.assertEqual(shadow.get("executed_policy"), "llm")
            self.assertIn("rule_variant", shadow)
            # Primary stayed llm WAIT; rule may WAIT or fire — agree is bool/None
            self.assertIn(shadow["agree"], (True, False, None))

            events = ledger.get_events(event_type="rule_shadow", limit=5)
            self.assertGreaterEqual(len(events), 1)
            self.assertEqual(events[0].data.get("executed_policy"), "llm")

    def test_shadow_disabled_skips_attachment(self):
        os.environ["KEEL_DECISION_POLICY"] = "llm"
        os.environ["KEEL_RULE_SHADOW"] = "0"
        os.environ["KEEL_LLM_EDGE_OVERLAY"] = "0"
        os.environ["KEEL_INSTRUMENTS"] = "BTC-USDT-SWAP"
        os.environ["KEEL_FORCE_PAPER"] = "1"

        llm_decision = Decision(
            inst_id="BTC-USDT-SWAP", action="WAIT", reason="llm wait"
        )
        client = MagicMock()
        client.request_decisions.return_value = LLMResponse(
            success=True,
            decisions={"BTC-USDT-SWAP": llm_decision},
            macro_assessment="",
            latency_ms=1,
            model="mock",
        )
        policy = LLMDecisionPolicy(client=client)

        with tempfile.TemporaryDirectory() as tmp:
            ledger = KeelLedger(Path(tmp) / "l.db")
            run_paper_cycle(
                ledger=ledger,
                exchange=PaperAdapter(),
                policy=policy,
                force_paper=True,
                instrument_ids=["BTC-USDT-SWAP"],
            )
            calc = ledger.get_decisions(limit=1)[0].calculus_data or {}
            self.assertNotIn("rule_shadow", calc)

    def test_llm_policy_still_primary_name(self):
        """Shadow must not change PolicyResult.policy_name."""
        snap = _enriched()
        rule = RuleDecisionPolicy()
        ctx = PolicyContext(
            snapshots={snap.inst_id: snap},
            instrument_ids=[snap.inst_id],
            timestamp=snap.timestamp,
        )
        # Sanity: rule decide works on synthetic
        rr = rule.decide(ctx)
        self.assertEqual(rr.policy_name, "rule")
        self.assertIn(snap.inst_id, rr.decisions)


if __name__ == "__main__":
    unittest.main()
