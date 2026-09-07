"""Q1: first-live tighter caps (KEEL_LIVE_MAX_*) + gate selection."""
from __future__ import annotations

import os
import unittest

from keel.config import refresh_settings
from keel.risk.gates import GateContext, MaxNotionalGate


class TestLiveCapsSettings(unittest.TestCase):
    def setUp(self):
        self._prev = {
            k: os.environ.get(k)
            for k in (
                "KEEL_OKX_ENV",
                "KEEL_SHADOW_MODE",
                "KEEL_MAX_NOTIONAL_PER_INSTRUMENT",
                "KEEL_MAX_CONTRACTS_PER_INSTRUMENT",
                "KEEL_LIVE_MAX_NOTIONAL_PER_INSTRUMENT",
                "KEEL_LIVE_MAX_CONTRACTS_PER_INSTRUMENT",
            )
        }
        for k in self._prev:
            os.environ.pop(k, None)
        refresh_settings()

    def tearDown(self):
        for k, v in self._prev.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v
        refresh_settings()

    def test_defaults(self):
        s = refresh_settings()
        self.assertEqual(s.live_max_notional_per_instrument, 200.0)
        self.assertEqual(s.live_max_contracts_per_instrument, 5)
        self.assertEqual(s.max_notional_per_instrument, 2000.0)
        self.assertEqual(s.max_contracts_per_instrument, 50)

    def test_paper_or_demo_uses_max_not_live(self):
        os.environ["KEEL_OKX_ENV"] = "demo"
        os.environ["KEEL_SHADOW_MODE"] = "0"
        s = refresh_settings()
        self.assertFalse(s.uses_live_caps)
        self.assertEqual(s.effective_max_notional_per_instrument, 2000.0)
        self.assertEqual(s.effective_max_contracts_per_instrument, 50)

    def test_live_shadow_keeps_max_caps(self):
        os.environ["KEEL_OKX_ENV"] = "live"
        os.environ["KEEL_SHADOW_MODE"] = "1"
        s = refresh_settings()
        self.assertFalse(s.uses_live_caps)
        self.assertEqual(s.effective_max_notional_per_instrument, s.max_notional_per_instrument)
        self.assertEqual(s.effective_max_contracts_per_instrument, s.max_contracts_per_instrument)

    def test_live_not_shadow_uses_live_caps(self):
        os.environ["KEEL_OKX_ENV"] = "live"
        os.environ["KEEL_SHADOW_MODE"] = "0"
        os.environ["KEEL_LIVE_MAX_NOTIONAL_PER_INSTRUMENT"] = "200"
        os.environ["KEEL_LIVE_MAX_CONTRACTS_PER_INSTRUMENT"] = "5"
        s = refresh_settings()
        self.assertTrue(s.uses_live_caps)
        self.assertEqual(s.effective_max_notional_per_instrument, 200.0)
        self.assertEqual(s.effective_max_contracts_per_instrument, 5)


class TestLiveCapsInGates(unittest.TestCase):
    def setUp(self):
        self._prev = {
            k: os.environ.get(k)
            for k in (
                "KEEL_OKX_ENV",
                "KEEL_SHADOW_MODE",
                "KEEL_LIVE_MAX_NOTIONAL_PER_INSTRUMENT",
                "KEEL_LIVE_MAX_CONTRACTS_PER_INSTRUMENT",
                "KEEL_MAX_NOTIONAL_PER_INSTRUMENT",
                "KEEL_MAX_CONTRACTS_PER_INSTRUMENT",
            )
        }
        for k in self._prev:
            os.environ.pop(k, None)
        refresh_settings()

    def tearDown(self):
        for k, v in self._prev.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v
        refresh_settings()

    def _ctx(self, notional: float, size: float = 0.0) -> GateContext:
        return GateContext(
            inst_id="BTC-USDT-SWAP",
            action="open_long",
            size=size,
            margin_required=50,
            current_positions=0,
            long_positions=0,
            short_positions=0,
            daily_pnl=0,
            notional=notional,
        )

    def test_gate_uses_live_caps_when_live_not_shadow(self):
        os.environ["KEEL_OKX_ENV"] = "live"
        os.environ["KEEL_SHADOW_MODE"] = "0"
        os.environ["KEEL_LIVE_MAX_NOTIONAL_PER_INSTRUMENT"] = "200"
        os.environ["KEEL_MAX_NOTIONAL_PER_INSTRUMENT"] = "2000"
        refresh_settings()
        gate = MaxNotionalGate()  # reads effective caps
        # 500 notional would pass KEEL_MAX_*=2000 but fail live 200
        result = gate.check(self._ctx(notional=500))
        self.assertFalse(result.passed)
        self.assertEqual(result.details.get("max_notional"), 200.0)

    def test_gate_uses_max_caps_when_shadow_on_live(self):
        os.environ["KEEL_OKX_ENV"] = "live"
        os.environ["KEEL_SHADOW_MODE"] = "1"
        os.environ["KEEL_LIVE_MAX_NOTIONAL_PER_INSTRUMENT"] = "200"
        os.environ["KEEL_MAX_NOTIONAL_PER_INSTRUMENT"] = "2000"
        refresh_settings()
        gate = MaxNotionalGate()
        result = gate.check(self._ctx(notional=500))
        self.assertTrue(result.passed)


class TestKillSwitchShadowGate(unittest.TestCase):
    def test_kill_gate_passes_when_shadow_mode_on_context(self):
        from keel.risk.gates import KillSwitchGate

        gate = KillSwitchGate(active=True)
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
            shadow_mode=True,
        )
        self.assertTrue(gate.check(ctx).passed)

    def test_kill_gate_blocks_when_shadow_off(self):
        from keel.risk.gates import KillSwitchGate

        gate = KillSwitchGate(active=True)
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
            shadow_mode=False,
        )
        self.assertFalse(gate.check(ctx).passed)


if __name__ == "__main__":
    unittest.main()
