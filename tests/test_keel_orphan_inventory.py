"""Orphan inventory helper + daily_loss_gate honesty status flag."""
from __future__ import annotations

import os
import tempfile
import time
import unittest
from pathlib import Path

from fastapi.testclient import TestClient

from keel.api.app import create_app
from keel.api.deps import set_ledger_path_override
from keel.config import refresh_settings
from keel.domain.records import TradeRecord
from keel.execution.close_reconcile import record_close_for_open
from keel.ledger import KeelLedger
from keel.ledger.orphan_inventory import daily_loss_gate_honesty, inventory_orphans


class TestOrphanInventory(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.db = Path(self.tmp.name) / "ledger.db"
        self.ledger = KeelLedger(self.db)

    def tearDown(self) -> None:
        self.ledger.close()
        self.tmp.cleanup()

    def _open(
        self,
        *,
        tag: str = "keel-llm",
        inst: str = "BTC-USDT-SWAP",
        ts: float | None = None,
    ) -> int:
        return self.ledger.record_trade(
            TradeRecord(
                timestamp=ts if ts is not None else time.time() - 100,
                inst_id=inst,
                action="open",
                direction="long",
                size=1.0,
                price=100.0,
                strategy_tag=tag,
                reason="test",
                metadata={"stop_loss": 95.0, "take_profit": 110.0},
            )
        )

    def test_counts_orphans_by_cohort(self) -> None:
        self._open(tag="keel-llm")
        self._open(tag="keel-shadow", inst="ETH-USDT-SWAP")
        self._open(tag="keel-shadow-near-probe", inst="SOL-USDT-SWAP")
        inv = inventory_orphans(self.ledger, now=time.time())
        self.assertEqual(inv.total, 3)
        self.assertEqual(inv.live, 1)
        self.assertEqual(inv.shadow, 2)
        self.assertEqual(inv.by_inst["BTC-USDT-SWAP"], 1)
        self.assertEqual(inv.by_inst["ETH-USDT-SWAP"], 1)

    def test_linked_close_removes_orphan(self) -> None:
        oid = self._open()
        opens = self.ledger.get_trades(action="open", limit=5)
        open_trade = next(t for t in opens if t.id == oid)
        outcome = record_close_for_open(
            self.ledger,
            open_trade=open_trade,
            exit_price=94.0,
            exit_reason="sl",
        )
        self.assertIsNotNone(outcome)
        inv = inventory_orphans(self.ledger)
        self.assertEqual(inv.total, 0)

    def test_daily_loss_gate_ineffective_without_closes(self) -> None:
        self._open()
        h = daily_loss_gate_honesty(self.ledger)
        self.assertFalse(h.effective)
        self.assertEqual(h.reason, "no_realized_closes")
        self.assertEqual(h.lifetime_close_count, 0)

    def test_daily_loss_gate_effective_after_realized_close(self) -> None:
        oid = self._open()
        opens = self.ledger.get_trades(action="open", limit=5)
        open_trade = next(t for t in opens if t.id == oid)
        record_close_for_open(
            self.ledger,
            open_trade=open_trade,
            exit_price=94.0,
            exit_reason="sl",
        )
        h = daily_loss_gate_honesty(self.ledger)
        self.assertTrue(h.effective)
        self.assertIsNone(h.reason)
        self.assertGreaterEqual(h.realized_close_count, 1)


class TestStatusOrphanFields(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.db = Path(self.tmp.name) / "ledger.db"
        self.ledger = KeelLedger(self.db)
        self.ledger.record_trade(
            TradeRecord(
                timestamp=time.time() - 50,
                inst_id="BTC-USDT-SWAP",
                action="open",
                direction="long",
                size=1.0,
                price=100.0,
                strategy_tag="keel-llm",
                reason="seed",
                metadata={},
            )
        )
        set_ledger_path_override(self.db)
        os.environ["KEEL_LEDGER_DB"] = str(self.db)
        refresh_settings()
        self.client = TestClient(create_app())

    def tearDown(self) -> None:
        self.ledger.close()
        set_ledger_path_override(None)
        os.environ.pop("KEEL_LEDGER_DB", None)
        refresh_settings()
        self.tmp.cleanup()

    def test_status_exposes_honesty_and_orphans(self) -> None:
        r = self.client.get("/api/v1/status")
        self.assertEqual(r.status_code, 200)
        body = r.json()
        self.assertIn("daily_loss_gate_effective", body)
        self.assertFalse(body["daily_loss_gate_effective"])
        self.assertEqual(body["daily_loss_gate_reason"], "no_realized_closes")
        orphans = body.get("ledger_orphans") or {}
        self.assertGreaterEqual(int(orphans.get("total") or 0), 1)
        self.assertGreaterEqual(int(orphans.get("live") or 0), 1)

    def test_ledger_orphans_endpoint(self) -> None:
        r = self.client.get("/api/v1/ledger/orphans?sample_limit=5")
        self.assertEqual(r.status_code, 200)
        body = r.json()
        self.assertGreaterEqual(body["total"], 1)
        self.assertTrue(body.get("sample"))
        self.assertIn("note", body)


if __name__ == "__main__":
    unittest.main()
