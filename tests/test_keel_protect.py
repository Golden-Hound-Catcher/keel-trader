"""Fee-aware SL protect: widen starved stops, BE only after a real R."""
from __future__ import annotations

import os
import unittest
from unittest.mock import patch

from keel.execution.protect import (
    close_fee_pad,
    favorable_r,
    plan_protect,
    price_tick_tolerance,
)


class TestProtectPlan(unittest.TestCase):
    def test_widen_tight_btc_stop(self) -> None:
        entry = 76742.8
        sl = 76841.2  # ~1 ATR leftover
        tp = 76526.4
        plan = plan_protect(
            side="short",
            entry=entry,
            mark=76707.0,
            sl=sl,
            tp=tp,
            atr=98.0,
        )
        self.assertIsNotNone(plan)
        assert plan is not None
        self.assertTrue(plan.widened)
        self.assertFalse(plan.breakeven)
        self.assertGreater(plan.new_sl, sl)
        self.assertLess(plan.new_tp, tp)
        self.assertGreaterEqual((plan.new_sl - entry) / (entry - plan.new_tp), 1 / 2.2 - 1e-6)

    def test_breakeven_after_half_r(self) -> None:
        entry = 77144.0
        sl_dist = entry * 0.001 * 6  # fee floor ~462
        sl = entry + sl_dist
        tp = entry - 2.2 * sl_dist
        mark = entry - 0.6 * sl_dist  # 0.6R in favor
        plan = plan_protect(
            side="short",
            entry=entry,
            mark=mark,
            sl=sl,
            tp=tp,
            atr=80.0,
        )
        self.assertIsNotNone(plan)
        assert plan is not None
        self.assertTrue(plan.breakeven)
        pad = close_fee_pad(entry)
        self.assertAlmostEqual(plan.new_sl, entry - pad)
        # Locked stop is still a tiny credit vs entry, not a fee-only TP target.
        self.assertLess(plan.new_sl, entry)

    def test_no_protect_when_already_wide_and_flat(self) -> None:
        entry = 2533.0
        geo_sl = max(2.0 * 20.0, entry * 0.001 * 6)
        sl = entry - geo_sl
        tp = entry + 2.2 * geo_sl
        plan = plan_protect(
            side="long",
            entry=entry,
            mark=entry + 1.0,
            sl=sl,
            tp=tp,
            atr=20.0,
        )
        self.assertIsNone(plan)

    def test_favorable_r_short(self) -> None:
        self.assertAlmostEqual(favorable_r("short", 100.0, 102.0, 99.0), 0.5)

    def test_be_ratchet_long_mark_pullback_keeps_be(self) -> None:
        """After BE lock, a later pass must not mirror SL back below entry."""
        entry = 86000.0
        pad = close_fee_pad(entry)
        be_sl = entry + pad  # ~86594-style lock relative to entry
        # Mark still green but pulled back — old bug recomputed entry-dist and
        # widened SL down into loss territory.
        mark = entry + pad * 2
        tp = entry + 2.2 * (entry * 0.001 * 6)
        plan = plan_protect(
            side="long",
            entry=entry,
            mark=mark,
            sl=be_sl,
            tp=tp,
            atr=80.0,
        )
        # Either no amend, or SL stays at/above locked BE (never below entry).
        if plan is None:
            return
        self.assertGreaterEqual(plan.new_sl, be_sl - 1e-9)
        self.assertGreaterEqual(plan.new_sl, entry)

    def test_be_ratchet_short_keeps_locked_be(self) -> None:
        entry = 77144.0
        pad = close_fee_pad(entry)
        be_sl = entry - pad
        mark = entry - pad * 2  # still favorable but softer
        tp = entry - 2.2 * (entry * 0.001 * 6)
        plan = plan_protect(
            side="short",
            entry=entry,
            mark=mark,
            sl=be_sl,
            tp=tp,
            atr=80.0,
        )
        if plan is None:
            return
        self.assertLessEqual(plan.new_sl, be_sl + 1e-9)
        self.assertLessEqual(plan.new_sl, entry)

    def test_be_then_recompute_never_below_entry_long(self) -> None:
        """Replay 9/23-shaped numbers: BE above entry must survive protect."""
        entry = 86000.0
        be_sl = 86594.0
        mark = 86650.0  # still green
        tp = 88000.0
        plan = plan_protect(
            side="long",
            entry=entry,
            mark=mark,
            sl=be_sl,
            tp=tp,
            atr=100.0,
        )
        if plan is not None:
            self.assertGreaterEqual(plan.new_sl, be_sl - 1e-6)
            self.assertNotAlmostEqual(plan.new_sl, 86031.0, places=0)

    def test_tick_rounded_be_short_not_reapplied(self) -> None:
        """9/25 #294 shape: OKX stores BE 84037.66015 as 84037.7 (BTC tick 0.1).

        Without a tick tolerance every cycle re-armed BE, re-amended the OCO
        and re-logged ``breakeven_plus_close_fee`` (event spam).
        """
        with patch.dict(os.environ, {"KEEL_LLM_BE_R": "1.0"}):
            plan = plan_protect(
                side="short",
                entry=84079.7,
                mark=83500.0,
                sl=84037.7,
                tp=82969.8,
                atr=250.0,
                inst_id="BTC-USDT-SWAP",
            )
        self.assertIsNone(plan)

    def test_tick_rounded_be_long_not_reapplied(self) -> None:
        with patch.dict(os.environ, {"KEEL_LLM_BE_R": "1.0"}):
            plan = plan_protect(
                side="long",
                entry=117.42,
                mark=119.5,
                sl=117.47,  # BE 117.47871 rounded down by a tick-sized amount
                tp=119.85,
                atr=0.4,
                inst_id="SOL-USDT-SWAP",
            )
        # 117.47 is 0.0087 below BE: more than half a 0.01 tick -> re-arm once.
        self.assertIsNotNone(plan)
        assert plan is not None
        self.assertTrue(plan.breakeven)
        with patch.dict(os.environ, {"KEEL_LLM_BE_R": "1.0"}):
            again = plan_protect(
                side="long",
                entry=117.42,
                mark=119.5,
                sl=117.48,  # exchange-rounded value of plan.new_sl
                tp=119.85,
                atr=0.4,
                inst_id="SOL-USDT-SWAP",
            )
        self.assertIsNone(again)

    def test_be_still_arms_first_time_with_inst_id(self) -> None:
        with patch.dict(os.environ, {"KEEL_LLM_BE_R": "1.0"}):
            plan = plan_protect(
                side="short",
                entry=84079.7,
                mark=83500.0,  # +1.15R
                sl=84584.18,
                tp=82969.8,
                atr=250.0,
                inst_id="BTC-USDT-SWAP",
            )
        self.assertIsNotNone(plan)
        assert plan is not None
        self.assertTrue(plan.breakeven)
        self.assertAlmostEqual(plan.new_sl, 84079.7 - close_fee_pad(84079.7))

    def test_price_tick_tolerance(self) -> None:
        self.assertAlmostEqual(price_tick_tolerance("BTC-USDT-SWAP"), 0.05)
        self.assertAlmostEqual(price_tick_tolerance("SOL-USDT-SWAP"), 0.005)
        self.assertEqual(price_tick_tolerance(None), 0.0)


if __name__ == "__main__":
    unittest.main()
