"""P2-5: order_sized → filled/failed/denied funnel helper."""
from __future__ import annotations

import tempfile
import time
import unittest
from pathlib import Path

from keel.ledger import KeelLedger

# Import helper from script module
import importlib.util

_ROOT = Path(__file__).resolve().parents[1]
_SPEC = importlib.util.spec_from_file_location(
    "llm_vs_rule_daily",
    _ROOT / "scripts" / "llm_vs_rule_daily.py",
)
_mod = importlib.util.module_from_spec(_SPEC)
assert _SPEC and _SPEC.loader
_SPEC.loader.exec_module(_mod)
sized_fill_funnel = _mod.sized_fill_funnel


class TestSizedFillFunnel(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.db = Path(self._tmp.name) / "ledger.db"
        self.ledger = KeelLedger(self.db)
        self.now = time.time()

    def tearDown(self) -> None:
        self.ledger.close()
        self._tmp.cleanup()

    def _ev(self, etype: str, decision_id: int | None, ts_offset: float = 0.0) -> None:
        data = {"action": "BUY_LONG"}
        if decision_id is not None:
            data["decision_id"] = decision_id
        # record_event API may not take timestamp — stamp via SQL if needed
        self.ledger.record_event(etype, inst_id="BTC-USDT-SWAP", data=data)

    def test_correlates_sized_to_filled_and_flags_unexplained(self):
        self._ev("order_sized", 1)
        self._ev("order_sized", 2)
        self._ev("order_sized", 3)
        self._ev("order_filled", 1)
        self._ev("order_failed", 2)
        # 3 remains unexplained
        report = sized_fill_funnel(self.ledger, hours=24)
        self.assertEqual(report["order_sized"], 3)
        self.assertEqual(report["sized_then_filled"], 1)
        self.assertEqual(report["sized_then_failed"], 1)
        self.assertEqual(report["unexplained_sized"], 1)
        self.assertIn(3, report["unexplained_decision_ids_sample"])


if __name__ == "__main__":
    unittest.main()
