"""Close reconcile: ledger closes when tracked positions vanish (mock OKX)."""
from __future__ import annotations

import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import MagicMock

from keel.domain.records import TradeRecord
from keel.exchange.protocol import Position, Ticker
from keel.execution.close_reconcile import (
    POSITIONS_SEEN_EVENT,
    infer_exit_reason,
    reconcile_closed_positions,
    realized_pnl,
)
from keel.ledger import KeelLedger


class TestInferExitReason(unittest.TestCase):
    def test_sl_by_proximity_long(self) -> None:
        reason = infer_exit_reason(
            side="long",
            entry=100.0,
            exit_price=94.9,
            stop_loss=95.0,
            take_profit=110.0,
        )
        self.assertEqual(reason, "sl")

    def test_tp_by_trigger_px(self) -> None:
        reason = infer_exit_reason(
            side="short",
            entry=100.0,
            exit_price=90.0,
            stop_loss=105.0,
            take_profit=90.0,
            algo={"triggerPx": "90", "slTriggerPx": "105", "tpTriggerPx": "90"},
        )
        self.assertEqual(reason, "tp")

    def test_pnl_long(self) -> None:
        # BTC-USDT-SWAP cv=0.01 → (110-100)*1*0.01 = 0.1
        pnl = realized_pnl(
            side="long",
            entry=100.0,
            exit_price=110.0,
            size=1.0,
            inst_id="BTC-USDT-SWAP",
        )
        self.assertAlmostEqual(pnl, 0.1)


class TestReconcileClosedPositions(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.db = Path(self.tmp.name) / "ledger.db"
        self.ledger = KeelLedger(self.db)

    def tearDown(self) -> None:
        self.ledger.close()
        self.tmp.cleanup()

    def _open_trade(self, *, price: float = 100.0, sl: float = 95.0, tp: float = 110.0) -> int:
        return self.ledger.record_trade(
            TradeRecord(
                timestamp=time.time() - 60,
                inst_id="BTC-USDT-SWAP",
                action="open",
                direction="long",
                size=1.0,
                price=price,
                strategy_tag="keel-llm",
                reason="test open",
                metadata={
                    "order_id": "ord-1",
                    "stop_loss": sl,
                    "take_profit": tp,
                },
            )
        )

    def _exchange(
        self,
        positions: list[Position],
        *,
        algos: list[dict] | None = None,
        last: float = 95.0,
    ) -> MagicMock:
        ex = MagicMock()
        ex.get_positions.return_value = positions
        ex.get_algo_history.return_value = list(algos or [])
        ex.get_ticker.return_value = Ticker(
            inst_id="BTC-USDT-SWAP",
            last=last,
            bid=last - 0.5,
            ask=last + 0.5,
        )
        return ex

    def test_baseline_then_sl_close(self) -> None:
        open_id = self._open_trade()
        live = [
            Position(
                inst_id="BTC-USDT-SWAP",
                side="long",
                size=1.0,
                avg_price=100.0,
                mark_price=99.0,
                leverage=3.0,
            )
        ]
        ex = self._exchange(live)
        # First cycle: baseline only
        out0 = reconcile_closed_positions(ex, self.ledger, now=time.time())
        self.assertEqual(out0, [])
        self.assertEqual(len(self.ledger.get_trades(action="close")), 0)
        seen = self.ledger.get_events(event_type=POSITIONS_SEEN_EVENT, limit=1)
        self.assertEqual(len(seen), 1)
        self.assertEqual(seen[0].data["positions"][0]["open_trade_ids"], [open_id])

        # Second cycle: position gone + SL algo fill
        algo = {
            "algoId": "algo-sl-1",
            "instId": "BTC-USDT-SWAP",
            "posSide": "long",
            "state": "effective",
            "actualPx": "94.8",
            "slTriggerPx": "95",
            "tpTriggerPx": "110",
            "triggerPx": "95",
            "uTime": str(int(time.time() * 1000)),
        }
        ex2 = self._exchange([], algos=[algo], last=94.8)
        out1 = reconcile_closed_positions(ex2, self.ledger, now=time.time())
        self.assertEqual(len(out1), 1)
        self.assertEqual(out1[0].exit_reason, "sl")
        self.assertEqual(out1[0].event_type, "sl_hit")
        self.assertEqual(out1[0].open_trade_id, open_id)

        closes = self.ledger.get_trades(action="close")
        self.assertEqual(len(closes), 1)
        self.assertEqual(closes[0].metadata["exit_reason"], "sl")
        self.assertEqual(closes[0].metadata["open_trade_id"], open_id)
        self.assertIsNotNone(closes[0].pnl)

        hits = self.ledger.get_events(event_type="sl_hit", limit=5)
        self.assertGreaterEqual(len(hits), 1)
        # sl_protect must remain a distinct type — we never emit it here
        self.assertNotEqual(hits[0].event_type, "sl_protect")

        # Idempotent: another reconcile does not duplicate
        out2 = reconcile_closed_positions(ex2, self.ledger, now=time.time())
        self.assertEqual(out2, [])
        self.assertEqual(len(self.ledger.get_trades(action="close")), 1)

    def test_no_invented_closes_for_orphan_opens(self) -> None:
        """Historical opens never tracked in positions_seen stay unmatched."""
        self._open_trade()
        ex = self._exchange([])  # flat book, no prior snapshot
        out = reconcile_closed_positions(ex, self.ledger, now=time.time())
        self.assertEqual(out, [])
        self.assertEqual(len(self.ledger.get_trades(action="close")), 0)
        # Baseline written empty
        seen = self.ledger.get_events(event_type=POSITIONS_SEEN_EVENT, limit=1)
        self.assertEqual(seen[0].data["count"], 0)

    def test_tp_hit_event(self) -> None:
        open_id = self._open_trade()
        live = [
            Position(
                inst_id="BTC-USDT-SWAP",
                side="long",
                size=1.0,
                avg_price=100.0,
                mark_price=105.0,
                leverage=3.0,
            )
        ]
        reconcile_closed_positions(self._exchange(live, last=105.0), self.ledger)
        algo = {
            "algoId": "algo-tp-1",
            "instId": "BTC-USDT-SWAP",
            "posSide": "long",
            "state": "effective",
            "actualPx": "110.2",
            "slTriggerPx": "95",
            "tpTriggerPx": "110",
            "triggerPx": "110",
            "uTime": str(int(time.time() * 1000)),
        }
        out = reconcile_closed_positions(
            self._exchange([], algos=[algo], last=110.2), self.ledger
        )
        self.assertEqual(len(out), 1)
        self.assertEqual(out[0].exit_reason, "tp")
        self.assertEqual(out[0].event_type, "tp_hit")
        self.assertEqual(out[0].open_trade_id, open_id)
        self.assertEqual(len(self.ledger.get_events(event_type="tp_hit")), 1)


    def test_positions_kwarg_skips_exchange_fetch(self) -> None:
        open_id = self._open_trade()
        live = [
            Position(
                inst_id="BTC-USDT-SWAP",
                side="long",
                size=1.0,
                avg_price=100.0,
                mark_price=99.0,
                leverage=3.0,
            )
        ]
        ex = self._exchange([])  # exchange flat; caller supplies live book
        out = reconcile_closed_positions(
            ex, self.ledger, now=time.time(), positions=live
        )
        self.assertEqual(out, [])
        ex.get_positions.assert_not_called()
        seen = self.ledger.get_events(event_type=POSITIONS_SEEN_EVENT, limit=1)
        self.assertEqual(seen[0].data["positions"][0]["open_trade_ids"], [open_id])


if __name__ == "__main__":
    unittest.main()
