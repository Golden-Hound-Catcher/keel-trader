"""E3 per-instrument full-gate rule fire cooldown."""
from __future__ import annotations

import os
import tempfile
import time
import unittest
from pathlib import Path

from keel.config import refresh_settings
from keel.domain.decision import Decision
from keel.domain.records import DecisionRecord
from keel.execution.fire_cooldown import (
    apply_rule_fire_cooldown,
    clamp_rule_fire_cooldown_seconds,
    full_gate_fire_recent,
    is_cooldown_fire,
)
from keel.ledger import KeelLedger
from keel.ledger.full_gate import is_full_gate_fire


class TestFireCooldownClamp(unittest.TestCase):
    def test_clamp_bounds(self):
        self.assertEqual(clamp_rule_fire_cooldown_seconds(900), 900)
        self.assertEqual(clamp_rule_fire_cooldown_seconds(0), 0)
        self.assertEqual(clamp_rule_fire_cooldown_seconds(-5), 0)
        self.assertEqual(clamp_rule_fire_cooldown_seconds(99999), 7200)

    def test_settings_loads_clamped(self):
        os.environ["KEEL_RULE_FIRE_COOLDOWN_SECONDS"] = "99999"
        try:
            s = refresh_settings()
            self.assertEqual(s.rule_fire_cooldown_seconds, 7200)
        finally:
            os.environ.pop("KEEL_RULE_FIRE_COOLDOWN_SECONDS", None)
            refresh_settings()


class TestFireCooldownApply(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.db = Path(self.temp.name) / "cooldown.db"
        self.ledger = KeelLedger(self.db)
        self.now = time.time()
        self.inst = "BTC-USDT-SWAP"
        self.diag = {
            "nearest": "short",
            "missing": [],
            "rule_variant": "trend_follow",
        }

    def tearDown(self):
        self.ledger.close()
        self.temp.cleanup()

    def _record_fire(self, ts: float, action: str = "SELL_SHORT") -> None:
        self.ledger.record_decision(
            DecisionRecord(
                timestamp=ts,
                inst_id=self.inst,
                action=action,
                confidence=70.0,
                entry_price=100.0,
                take_profit=95.0,
                stop_loss=102.0,
                reason="full gate",
                policy_name="rule",
                calculus_data={
                    "market_source": "okx_public",
                    "signal_diag": dict(self.diag),
                },
            )
        )

    def test_second_full_gate_within_window_becomes_wait(self):
        self._record_fire(self.now - 60)
        self.assertTrue(
            full_gate_fire_recent(
                self.ledger,
                inst_id=self.inst,
                cooldown_seconds=900,
                now=self.now,
            )
        )
        candidate = Decision(
            inst_id=self.inst,
            action="SELL_SHORT",
            confidence=70.0,
            entry_price=100.0,
            take_profit=95.0,
            stop_loss=102.0,
            reason="would fire",
            signal_diag=dict(self.diag),
        )
        self.assertTrue(
            is_full_gate_fire(
                candidate.action, candidate.signal_diag, policy_name="rule"
            )
        )
        out = apply_rule_fire_cooldown(
            candidate,
            ledger=self.ledger,
            cooldown_seconds=900,
            now=self.now,
            policy_name="rule",
        )
        self.assertEqual(out.action, "WAIT")
        self.assertIn("cooldown", out.reason.lower())
        self.assertIsNotNone(out.signal_diag)
        self.assertTrue(out.signal_diag.get("fire_cooldown_active"))
        self.assertEqual(out.signal_diag.get("fire_cooldown_seconds"), 900)
        # Suppressed WAIT must not count as full_gate_fire
        self.assertFalse(
            is_full_gate_fire(out.action, out.signal_diag, policy_name="rule")
        )
        self.assertEqual(out.signal_diag.get("nearest"), "none")
        self.assertEqual(out.signal_diag.get("nearest_before_suppress"), "short")
        self.assertIn("fire_cooldown_ok", out.signal_diag.get("missing") or [])
        from keel.execution.near_probe import near_signal_meets_gates, evaluate_near_probe
        from keel.factors.market_data import MarketSnapshot

        self.assertFalse(near_signal_meets_gates(out.signal_diag, max_missing=2))
        snap = MarketSnapshot(
            inst_id=self.inst,
            name="BTC",
            timestamp=self.now,
            price=100.0,
            atr_14=1.0,
            data_valid=True,
        )
        probe = evaluate_near_probe(
            out,
            snap,
            kill_switch=True,
            shadow_mode=True,
            probe_enabled=True,
        )
        self.assertFalse(probe.fired)
        self.assertEqual(probe.skip_reason, "not_near")

    def test_after_window_can_fire_again(self):
        self._record_fire(self.now - 1000)
        self.assertFalse(
            full_gate_fire_recent(
                self.ledger,
                inst_id=self.inst,
                cooldown_seconds=900,
                now=self.now,
            )
        )
        candidate = Decision(
            inst_id=self.inst,
            action="BUY_LONG",
            confidence=70.0,
            entry_price=100.0,
            take_profit=105.0,
            stop_loss=98.0,
            reason="fire again",
            signal_diag={"nearest": "long", "missing": []},
        )
        out = apply_rule_fire_cooldown(
            candidate,
            ledger=self.ledger,
            cooldown_seconds=900,
            now=self.now,
            policy_name="rule",
        )
        self.assertEqual(out.action, "BUY_LONG")
        self.assertFalse((out.signal_diag or {}).get("fire_cooldown_active"))

    def test_zero_disables(self):
        self._record_fire(self.now - 10)
        candidate = Decision(
            inst_id=self.inst,
            action="SELL_SHORT",
            confidence=70.0,
            entry_price=100.0,
            take_profit=95.0,
            stop_loss=102.0,
            reason="fire",
            signal_diag=dict(self.diag),
        )
        out = apply_rule_fire_cooldown(
            candidate,
            ledger=self.ledger,
            cooldown_seconds=0,
            now=self.now,
            policy_name="rule",
        )
        self.assertEqual(out.action, "SELL_SHORT")

    def test_other_instrument_not_blocked(self):
        self._record_fire(self.now - 30)
        candidate = Decision(
            inst_id="SOL-USDT-SWAP",
            action="SELL_SHORT",
            confidence=70.0,
            entry_price=10.0,
            take_profit=9.5,
            stop_loss=10.2,
            reason="other inst",
            signal_diag=dict(self.diag),
        )
        out = apply_rule_fire_cooldown(
            candidate,
            ledger=self.ledger,
            cooldown_seconds=900,
            now=self.now,
            policy_name="rule",
        )
        self.assertEqual(out.action, "SELL_SHORT")


    def test_llm_veto_confirmed_fire_enters_cooldown(self):
        self.ledger.record_decision(
            DecisionRecord(
                timestamp=self.now - 60,
                inst_id=self.inst,
                action="SELL_SHORT",
                confidence=70.0,
                entry_price=100.0,
                take_profit=95.0,
                stop_loss=102.0,
                reason="llm confirm",
                policy_name="llm_veto",
                calculus_data={
                    "market_source": "okx_public",
                    "signal_diag": dict(self.diag),
                },
            )
        )
        self.assertTrue(
            full_gate_fire_recent(
                self.ledger,
                inst_id=self.inst,
                cooldown_seconds=900,
                now=self.now,
            )
        )
        candidate = Decision(
            inst_id=self.inst,
            action="SELL_SHORT",
            confidence=70.0,
            entry_price=100.0,
            take_profit=95.0,
            stop_loss=102.0,
            reason="would fire again",
            signal_diag=dict(self.diag),
        )
        out = apply_rule_fire_cooldown(
            candidate,
            ledger=self.ledger,
            cooldown_seconds=900,
            now=self.now,
            policy_name="llm_veto",
        )
        self.assertEqual(out.action, "WAIT")
        self.assertTrue(out.signal_diag.get("fire_cooldown_active"))

    def test_llm_buy_enters_cooldown_without_full_gate(self):
        llm_diag = {"reason": "model"}
        self.assertFalse(
            is_full_gate_fire("BUY_LONG", llm_diag, policy_name="llm")
        )
        self.assertTrue(
            is_cooldown_fire("BUY_LONG", llm_diag, policy_name="llm")
        )
        self.ledger.record_decision(
            DecisionRecord(
                timestamp=self.now - 60,
                inst_id=self.inst,
                action="BUY_LONG",
                confidence=70.0,
                entry_price=100.0,
                take_profit=105.0,
                stop_loss=98.0,
                reason="llm fire",
                policy_name="llm",
                calculus_data={"signal_diag": llm_diag},
            )
        )
        self.assertTrue(
            full_gate_fire_recent(
                self.ledger,
                inst_id=self.inst,
                cooldown_seconds=900,
                now=self.now,
            )
        )
        candidate = Decision(
            inst_id=self.inst,
            action="BUY_LONG",
            confidence=70.0,
            entry_price=100.0,
            take_profit=105.0,
            stop_loss=98.0,
            reason="would spray",
            signal_diag=dict(llm_diag),
        )
        out = apply_rule_fire_cooldown(
            candidate,
            ledger=self.ledger,
            cooldown_seconds=900,
            now=self.now,
            policy_name="llm",
        )
        self.assertEqual(out.action, "WAIT")
        self.assertTrue(out.signal_diag.get("fire_cooldown_active"))
        self.assertFalse(
            is_full_gate_fire(out.action, out.signal_diag, policy_name="llm")
        )
        self.assertFalse(
            is_cooldown_fire(out.action, out.signal_diag, policy_name="llm")
        )

    def test_llm_wait_does_not_start_cooldown(self):
        self.ledger.record_decision(
            DecisionRecord(
                timestamp=self.now - 30,
                inst_id=self.inst,
                action="WAIT",
                confidence=40.0,
                reason="htf block",
                policy_name="llm",
                calculus_data={"signal_diag": {"missing": ["htf_ok"]}},
            )
        )
        candidate = Decision(
            inst_id=self.inst,
            action="BUY_LONG",
            confidence=70.0,
            entry_price=100.0,
            take_profit=105.0,
            stop_loss=98.0,
            reason="aligned fire",
        )
        out = apply_rule_fire_cooldown(
            candidate,
            ledger=self.ledger,
            cooldown_seconds=900,
            now=self.now,
            policy_name="llm",
        )
        self.assertEqual(out.action, "BUY_LONG")

    def test_llm_reentry_blocks_beyond_900s(self):
        self.ledger.record_decision(
            DecisionRecord(
                timestamp=self.now - 1000,
                inst_id=self.inst,
                action="BUY_LONG",
                confidence=70.0,
                entry_price=100.0,
                take_profit=105.0,
                stop_loss=98.0,
                reason="llm fire",
                policy_name="llm",
                calculus_data={"signal_diag": {"reason": "model"}},
            )
        )
        candidate = Decision(
            inst_id=self.inst,
            action="BUY_LONG",
            confidence=70.0,
            entry_price=100.0,
            take_profit=105.0,
            stop_loss=98.0,
            reason="would spray after sl",
        )
        out = apply_rule_fire_cooldown(
            candidate,
            ledger=self.ledger,
            cooldown_seconds=900,
            now=self.now,
            policy_name="llm",
        )
        self.assertEqual(out.action, "WAIT")
        self.assertTrue(out.signal_diag.get("fire_cooldown_active"))
        self.assertGreaterEqual(
            int(out.signal_diag.get("fire_cooldown_seconds") or 0), 3600
        )


if __name__ == "__main__":
    unittest.main()
