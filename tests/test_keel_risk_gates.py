"""Tests for keel.risk.gates module - hard risk gates."""
import unittest
import time

from keel.risk.gates import (
    GateContext,
    GateResult,
    MaxPositionsGate,
    MaxSameDirectionGate,
    DailyLossGate,
    MaxMarginGate,
    CooldownGate,
    KillSwitchGate,
    check_all_gates,
    get_default_gates,
)


class TestMaxPositionsGate(unittest.TestCase):
    """Tests for max positions gate."""

    def test_allows_within_limit(self):
        gate = MaxPositionsGate(max_positions=6)
        ctx = GateContext(
            inst_id="BTC-USDT-SWAP",
            action="open_long",
            size=1,
            margin_required=100,
            current_positions=3,
            long_positions=2,
            short_positions=1,
            daily_pnl=0,
        )
        result = gate.check(ctx)
        self.assertTrue(result.passed)

    def test_blocks_at_limit(self):
        gate = MaxPositionsGate(max_positions=6)
        ctx = GateContext(
            inst_id="BTC-USDT-SWAP",
            action="open_long",
            size=1,
            margin_required=100,
            current_positions=6,
            long_positions=3,
            short_positions=3,
            daily_pnl=0,
        )
        result = gate.check(ctx)
        self.assertFalse(result.passed)
        self.assertIn("最大持仓数", result.reason)

    def test_allows_close(self):
        gate = MaxPositionsGate(max_positions=6)
        ctx = GateContext(
            inst_id="BTC-USDT-SWAP",
            action="close",
            size=1,
            margin_required=0,
            current_positions=6,
            long_positions=3,
            short_positions=3,
            daily_pnl=0,
        )
        result = gate.check(ctx)
        self.assertTrue(result.passed)


class TestDailyLossGate(unittest.TestCase):
    """Tests for daily loss gate."""

    def test_allows_positive_pnl(self):
        gate = DailyLossGate(max_daily_loss=150)
        ctx = GateContext(
            inst_id="BTC-USDT-SWAP",
            action="open_long",
            size=1,
            margin_required=100,
            current_positions=0,
            long_positions=0,
            short_positions=0,
            daily_pnl=50,
        )
        result = gate.check(ctx)
        self.assertTrue(result.passed)

    def test_allows_small_loss(self):
        gate = DailyLossGate(max_daily_loss=150)
        ctx = GateContext(
            inst_id="BTC-USDT-SWAP",
            action="open_long",
            size=1,
            margin_required=100,
            current_positions=0,
            long_positions=0,
            short_positions=0,
            daily_pnl=-100,
        )
        result = gate.check(ctx)
        self.assertTrue(result.passed)

    def test_blocks_at_limit(self):
        gate = DailyLossGate(max_daily_loss=150)
        ctx = GateContext(
            inst_id="BTC-USDT-SWAP",
            action="open_long",
            size=1,
            margin_required=100,
            current_positions=0,
            long_positions=0,
            short_positions=0,
            daily_pnl=-160,
        )
        result = gate.check(ctx)
        self.assertFalse(result.passed)
        self.assertIn("亏损", result.reason)

    def test_allows_close_even_at_limit(self):
        gate = DailyLossGate(max_daily_loss=150)
        ctx = GateContext(
            inst_id="BTC-USDT-SWAP",
            action="close",
            size=1,
            margin_required=0,
            current_positions=1,
            long_positions=1,
            short_positions=0,
            daily_pnl=-200,
        )
        result = gate.check(ctx)
        self.assertTrue(result.passed)


class TestMaxMarginGate(unittest.TestCase):
    """Tests for max asset margin gate."""

    def test_allows_within_limit(self):
        gate = MaxMarginGate(max_margin=600)
        ctx = GateContext(
            inst_id="BTC-USDT-SWAP",
            action="open_long",
            size=1,
            margin_required=100,
            current_positions=0,
            long_positions=0,
            short_positions=0,
            daily_pnl=0,
            existing_margin_for_asset=0,
        )
        result = gate.check(ctx)
        self.assertTrue(result.passed)

    def test_allows_scale_in_within_limit(self):
        gate = MaxMarginGate(max_margin=600)
        ctx = GateContext(
            inst_id="BTC-USDT-SWAP",
            action="scale_in",
            size=1,
            margin_required=200,
            current_positions=1,
            long_positions=1,
            short_positions=0,
            daily_pnl=0,
            existing_margin_for_asset=300,
        )
        result = gate.check(ctx)
        self.assertTrue(result.passed)

    def test_blocks_over_limit(self):
        gate = MaxMarginGate(max_margin=600)
        ctx = GateContext(
            inst_id="BTC-USDT-SWAP",
            action="scale_in",
            size=1,
            margin_required=400,
            current_positions=1,
            long_positions=1,
            short_positions=0,
            daily_pnl=0,
            existing_margin_for_asset=300,
        )
        result = gate.check(ctx)
        self.assertFalse(result.passed)
        self.assertIn("保证金", result.reason)


class TestCooldownGate(unittest.TestCase):
    """Tests for cooldown gate."""

    def test_allows_no_cooldown(self):
        gate = CooldownGate()
        ctx = GateContext(
            inst_id="BTC-USDT-SWAP",
            action="open_long",
            size=1,
            margin_required=100,
            current_positions=0,
            long_positions=0,
            short_positions=0,
            daily_pnl=0,
            cooldown_until=0,
        )
        result = gate.check(ctx)
        self.assertTrue(result.passed)

    def test_allows_expired_cooldown(self):
        gate = CooldownGate()
        ctx = GateContext(
            inst_id="BTC-USDT-SWAP",
            action="open_long",
            size=1,
            margin_required=100,
            current_positions=0,
            long_positions=0,
            short_positions=0,
            daily_pnl=0,
            cooldown_until=time.time() - 100,
        )
        result = gate.check(ctx)
        self.assertTrue(result.passed)

    def test_blocks_during_cooldown(self):
        gate = CooldownGate()
        ctx = GateContext(
            inst_id="BTC-USDT-SWAP",
            action="open_long",
            size=1,
            margin_required=100,
            current_positions=0,
            long_positions=0,
            short_positions=0,
            daily_pnl=0,
            cooldown_until=time.time() + 600,
        )
        result = gate.check(ctx)
        self.assertFalse(result.passed)
        self.assertIn("冷却", result.reason)


class TestKillSwitchGate(unittest.TestCase):
    """Tests for kill switch gate."""

    def test_allows_normal(self):
        gate = KillSwitchGate()
        ctx = GateContext(
            inst_id="BTC-USDT-SWAP",
            action="open_long",
            size=1,
            margin_required=100,
            current_positions=0,
            long_positions=0,
            short_positions=0,
            daily_pnl=0,
            kill_switch_active=False,
        )
        result = gate.check(ctx)
        self.assertTrue(result.passed)

    def test_blocks_when_active(self):
        gate = KillSwitchGate()
        ctx = GateContext(
            inst_id="BTC-USDT-SWAP",
            action="open_long",
            size=1,
            margin_required=100,
            current_positions=0,
            long_positions=0,
            short_positions=0,
            daily_pnl=0,
            kill_switch_active=True,
        )
        result = gate.check(ctx)
        self.assertFalse(result.passed)
        self.assertIn("熔断", result.reason)


class TestCheckAllGates(unittest.TestCase):
    """Tests for check_all_gates function."""

    def test_all_pass(self):
        ctx = GateContext(
            inst_id="BTC-USDT-SWAP",
            action="open_long",
            size=1,
            margin_required=100,
            current_positions=0,
            long_positions=0,
            short_positions=0,
            daily_pnl=0,
        )
        passed, results = check_all_gates(ctx)
        self.assertTrue(passed)
        self.assertTrue(all(r.passed for r in results))

    def test_stops_at_first_failure(self):
        ctx = GateContext(
            inst_id="BTC-USDT-SWAP",
            action="open_long",
            size=1,
            margin_required=100,
            current_positions=0,
            long_positions=0,
            short_positions=0,
            daily_pnl=0,
            kill_switch_active=True,
        )
        passed, results = check_all_gates(ctx)
        self.assertFalse(passed)
        self.assertEqual(len(results), 1)
        self.assertEqual(results[0].gate_name, "kill_switch")


if __name__ == "__main__":
    unittest.main()
