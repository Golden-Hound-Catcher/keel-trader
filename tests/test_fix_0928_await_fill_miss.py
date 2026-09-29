"""9/28 #306/#307/#308: transient get_order miss must not fall back to ledger-at-acceptance.

OKX had created the attached OCO for all three, but ``await_fill`` returned None
on a lookup miss, the orchestrator took the legacy path (limit px, fee 0, one
pending-OCO probe, no retry, no fallback) and logged a false
``sl_tp_attach_failed=no_pending_oco``.
"""
from __future__ import annotations

import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from keel.execution.entry_fill import (
    ORDER_RESTING_EVENT,
    UNKNOWN_STATE,
    await_fill,
    reconcile_resting_entries,
)
from keel.execution.orchestrator import ExecutionOrchestrator
from keel.ledger import KeelLedger

from tests.test_pm_0924_fill_reconcile import INST, _decision, _order_row, _OkxLikeExchange


class _MissThenRows(_OkxLikeExchange):
    """get_order returns None for the first ``misses`` calls (OKX 51603 / HTTP blip)."""

    def __init__(self, misses: int) -> None:
        super().__init__()
        self.misses = misses
        self.calls = 0

    def get_order(self, inst_id: str, order_id: str) -> dict | None:
        self.calls += 1
        if self.calls <= self.misses:
            return None
        return super().get_order(inst_id, order_id)


class TestAwaitFillLookupMiss(unittest.TestCase):
    def test_miss_then_fill_keeps_polling(self) -> None:
        ex = _MissThenRows(misses=2)
        ex.order_rows = [_order_row("filled", fill=1, px=83397.8, fill_ms=1_790_568_944_000, fee=-0.41)]
        info = await_fill(ex, INST, "ord-9", wait_seconds=5, poll_seconds=1, sleep=lambda s: None)
        assert info is not None
        self.assertTrue(info.fully_filled)
        self.assertAlmostEqual(info.avg_px, 83397.8)
        self.assertEqual(ex.calls, 3)

    def test_persistent_miss_returns_unknown_not_none(self) -> None:
        ex = _MissThenRows(misses=99)
        info = await_fill(ex, INST, "ord-9", wait_seconds=3, poll_seconds=1, sleep=lambda s: None)
        assert info is not None
        self.assertEqual(info.state, UNKNOWN_STATE)
        self.assertFalse(info.terminal)
        self.assertFalse(info.fully_filled)
        self.assertEqual(info.filled_size, 0.0)

    def test_unknown_even_with_zero_budget(self) -> None:
        ex = _MissThenRows(misses=99)
        info = await_fill(ex, INST, "ord-9", wait_seconds=0)
        assert info is not None
        self.assertEqual(info.state, UNKNOWN_STATE)

    def test_no_order_id_or_no_getter_is_none(self) -> None:
        self.assertIsNone(await_fill(object(), INST, "x", wait_seconds=0))
        self.assertIsNone(await_fill(_OkxLikeExchange(), INST, None, wait_seconds=0))


class TestOrchestratorLookupMiss(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.ledger = KeelLedger(Path(self.tmp.name) / "l.db")
        self._env = patch.dict(os.environ, {"KEEL_ENTRY_FILL_WAIT_SECONDS": "0"}, clear=False)
        self._env.start()
        self._sleep = patch("keel.execution.entry_fill.time.sleep", lambda s: None)
        self._sleep.start()

    def tearDown(self) -> None:
        self._sleep.stop()
        self._env.stop()
        self.ledger.close()
        self.tmp.cleanup()

    def test_lookup_miss_parks_resting_then_reconcile_records_fill_truth(self) -> None:
        ex = _MissThenRows(misses=99)
        orch = ExecutionOrchestrator(exchange=ex, ledger=self.ledger)  # type: ignore[arg-type]
        res = orch.execute_decision(_decision(), kill_switch=False, shadow_mode=False)
        self.assertTrue(res.success)
        self.assertTrue(res.resting)
        self.assertFalse(res.filled)
        # No acceptance-time open and no false attach failure.
        self.assertEqual(self.ledger.get_trades(action="open"), [])
        self.assertEqual(self.ledger.get_events(event_type="sl_tp_attach_failed"), [])
        self.assertEqual(ex.placed_oco, [])
        resting = self.ledger.get_events(event_type=ORDER_RESTING_EVENT)
        self.assertEqual(len(resting), 1)
        data = resting[0].data or {}
        self.assertEqual(data.get("state"), UNKNOWN_STATE)
        self.assertEqual((data.get("ctx") or {}).get("direction"), "short")

        # Next cycle: lookup works, order filled, attached OCO visible.
        ex.misses = 0
        ex.order_rows = [_order_row("filled", fill=1, px=84012.3, fill_ms=1_790_568_944_000, fee=-0.42)]
        ex.algos = [{"instId": INST, "posSide": "short", "algoId": "oco-306"}]
        out = reconcile_resting_entries(ex, self.ledger, record_open=orch.record_filled_open, ttl_seconds=900)
        self.assertEqual([r["outcome"] for r in out], ["filled"])
        trade = self.ledger.get_trades(action="open", limit=1)[0]
        self.assertAlmostEqual(trade.price, 84012.3)
        self.assertAlmostEqual(trade.fee, 0.42)
        meta = trade.metadata or {}
        self.assertEqual(meta.get("fill_source"), "okx_order")
        self.assertTrue(meta.get("sl_tp_attached"))
        self.assertEqual(meta.get("algo_ids"), ["oco-306"])
        self.assertEqual(self.ledger.get_events(event_type="sl_tp_attach_failed"), [])
        self.assertEqual(ex.placed_oco, [])

    def test_unknown_resting_is_not_cancelled_blindly_by_ttl(self) -> None:
        ex = _MissThenRows(misses=99)
        orch = ExecutionOrchestrator(exchange=ex, ledger=self.ledger)  # type: ignore[arg-type]
        orch.execute_decision(_decision(), kill_switch=False, shadow_mode=False)
        out = reconcile_resting_entries(
            ex, self.ledger, record_open=orch.record_filled_open, ttl_seconds=1, now=4_000_000_000.0
        )
        self.assertEqual(out, [])
        self.assertEqual(ex.cancelled, [])


if __name__ == "__main__":
    unittest.main()
