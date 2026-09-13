"""Fee-aware SL protect: widen starved stops, BE only after a real R."""
from __future__ import annotations

import unittest

from keel.execution.protect import close_fee_pad, favorable_r, plan_protect


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


if __name__ == "__main__":
    unittest.main()
