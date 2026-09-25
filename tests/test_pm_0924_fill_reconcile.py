"""9/24 post-mortem: fill truth, resting entries, TP/SL repair, close reconcile, knobs."""
from __future__ import annotations

import json
import os
import tempfile
import time
import unittest
from pathlib import Path
from typing import Any
from unittest.mock import patch

from keel.config.profiles import profile_defaults
from keel.domain.decision import Decision
from keel.domain.records import TradeRecord
from keel.exchange.okx_rest import OKXRestAdapter
from keel.exchange.protocol import AccountBalance, Order, OrderResult, Position, Ticker
from keel.execution.close_reconcile import (
    POSITIONS_SEEN_EVENT,
    match_history_close,
    reconcile_closed_positions,
)
from keel.execution.entry_fill import (
    ORDER_RESOLVED_EVENT,
    ORDER_RESTING_EVENT,
    await_fill,
    parse_order_row,
    reconcile_resting_entries,
)
from keel.execution.orchestrator import ExecutionOrchestrator
from keel.execution.protect import be_trigger_r, plan_protect
from keel.ledger import KeelLedger

INST = "BTC-USDT-SWAP"


def _order_row(state: str, *, fill: float = 0.0, px: float = 0.0, fill_ms: int = 0, fee: float = 0.0) -> dict:
    return {
        "state": state,
        "accFillSz": str(fill),
        "avgPx": str(px) if px else "",
        "fillTime": str(fill_ms) if fill_ms else "",
        "fee": str(fee),
        "attachAlgoOrds": [{"attachAlgoId": "a1", "failCode": "", "failReason": ""}],
    }


class _OkxLikeExchange:
    """Stub with the OKX-only surface: get_order / pending oco / oco repair / cancel."""

    def __init__(self) -> None:
        self.order_rows: list[dict] = []  # successive get_order responses (last repeats)
        self.algos: list[dict] = []
        self.placed_oco: list[tuple] = []
        self.cancelled: list[str] = []
        self.positions: list[Position] = []
        self.history: list[dict] = []

    # --- ExchangeProtocol bits used by orchestrator
    def get_ticker(self, inst_id: str) -> Ticker:
        return Ticker(inst_id=inst_id, last=84000.0, bid=83999.9, ask=84000.1)

    def get_positions(self) -> list[Position]:
        return list(self.positions)

    def get_balance(self) -> AccountBalance:
        return AccountBalance(
            total_equity=10_000.0, available_balance=10_000.0, cash_balance=10_000.0, unrealized_pnl=0.0
        )

    def place_order(self, request) -> OrderResult:
        return OrderResult(
            success=True,
            order_id="ord-9",
            order=Order(
                order_id="ord-9",
                inst_id=request.inst_id,
                side=request.side,
                pos_side=request.pos_side,
                order_type="limit",
                size=request.size,
                price=request.price,
                state="live",
            ),
        )

    # --- OKX extras
    def get_order(self, inst_id: str, order_id: str) -> dict | None:
        if not self.order_rows:
            return None
        return self.order_rows.pop(0) if len(self.order_rows) > 1 else self.order_rows[0]

    def get_pending_oco(self, inst_id: str | None = None) -> list[dict]:
        return list(self.algos)

    def place_oco_tpsl(self, inst_id: str, pos_side: str, *, sl=None, tp=None) -> str | None:
        self.placed_oco.append((inst_id, pos_side, sl, tp))
        return "repair-1"

    def cancel_order(self, inst_id: str, order_id: str) -> bool:
        self.cancelled.append(order_id)
        self.order_rows = [_order_row("canceled")]
        return True

    def get_positions_history(self, inst_id: str | None = None, *, limit: int = 100) -> list[dict]:
        return list(self.history)

    def get_algo_history(self, inst_id: str | None = None, *, limit: int = 50) -> list[dict]:
        return []


def _decision() -> Decision:
    return Decision(
        inst_id=INST,
        action="SELL_SHORT",
        confidence=72,
        entry_price=84000.0,
        take_profit=82890.0,
        stop_loss=84505.0,
        leverage=5,
        margin_usdt=170.0,
        reason="pm unit short",
    )


class _LedgerCase(unittest.TestCase):
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


class TestFillParsing(unittest.TestCase):
    def test_parse_and_poll_until_filled(self) -> None:
        ex = _OkxLikeExchange()
        ex.order_rows = [
            _order_row("live"),
            _order_row("live"),
            _order_row("filled", fill=1, px=84010.5, fill_ms=1_790_000_000_000, fee=-0.42),
        ]
        info = await_fill(ex, INST, "ord-9", wait_seconds=5, poll_seconds=1, sleep=lambda s: None)
        assert info is not None
        self.assertTrue(info.fully_filled)
        self.assertAlmostEqual(info.avg_px, 84010.5)
        self.assertAlmostEqual(info.fill_ts or 0, 1_790_000_000.0)
        self.assertAlmostEqual(info.fee, -0.42)

    def test_unsupported_adapter_returns_none(self) -> None:
        self.assertIsNone(await_fill(object(), INST, "x", wait_seconds=0))
        self.assertIsNone(parse_order_row(None))


class TestOrchestratorFillTruth(_LedgerCase):
    def _orch(self, ex: _OkxLikeExchange) -> ExecutionOrchestrator:
        return ExecutionOrchestrator(exchange=ex, ledger=self.ledger)  # type: ignore[arg-type]

    def test_immediate_fill_uses_avg_px_and_fee(self) -> None:
        ex = _OkxLikeExchange()
        ex.order_rows = [_order_row("filled", fill=1, px=84075.1, fill_ms=1_790_000_100_000, fee=-0.42)]
        ex.algos = [{"instId": INST, "posSide": "short", "algoId": "oco-1"}]
        res = self._orch(ex).execute_decision(_decision(), kill_switch=False, shadow_mode=False)
        self.assertTrue(res.filled)
        trade = self.ledger.get_trades(action="open", limit=1)[0]
        self.assertAlmostEqual(trade.price, 84075.1)  # avgPx, not the 84000 limit
        self.assertAlmostEqual(trade.fee, 0.42)
        self.assertAlmostEqual(trade.timestamp, 1_790_000_100.0)
        meta = trade.metadata or {}
        self.assertEqual(meta.get("limit_px"), 84000.0)
        self.assertTrue(meta.get("sl_tp_attached"))
        self.assertEqual(len(self.ledger.get_events(event_type="sl_tp_attach_failed")), 0)

    def test_resting_order_is_not_a_trade_until_filled(self) -> None:
        ex = _OkxLikeExchange()
        ex.order_rows = [_order_row("live")]
        orch = self._orch(ex)
        res = orch.execute_decision(_decision(), kill_switch=False, shadow_mode=False)
        self.assertTrue(res.resting)
        self.assertFalse(res.filled)
        self.assertEqual(self.ledger.get_trades(action="open"), [])
        # No false "attach failed" while the parent is merely resting.
        self.assertEqual(len(self.ledger.get_events(event_type="sl_tp_attach_failed")), 0)
        resting = self.ledger.get_events(event_type=ORDER_RESTING_EVENT)
        self.assertEqual(len(resting), 1)
        self.assertEqual((resting[0].data or {}).get("ctx", {}).get("direction"), "short")

        # Later cycle: filled 11 min after placement.
        ex.order_rows = [_order_row("filled", fill=1, px=84000.0, fill_ms=1_790_000_700_000, fee=-0.168)]
        ex.algos = [{"instId": INST, "posSide": "short", "algoId": "oco-2"}]
        out = reconcile_resting_entries(ex, self.ledger, record_open=orch.record_filled_open, ttl_seconds=900)
        self.assertEqual([r["outcome"] for r in out], ["filled"])
        trades = self.ledger.get_trades(action="open")
        self.assertEqual(len(trades), 1)
        self.assertAlmostEqual(trades[0].timestamp, 1_790_000_700.0)
        # Idempotent: resolved order is not replayed.
        again = reconcile_resting_entries(ex, self.ledger, record_open=orch.record_filled_open, ttl_seconds=900)
        self.assertEqual(again, [])
        self.assertEqual(len(self.ledger.get_trades(action="open")), 1)

    def test_resting_past_ttl_is_cancelled_without_trade(self) -> None:
        ex = _OkxLikeExchange()
        ex.order_rows = [_order_row("live")]
        orch = self._orch(ex)
        orch.execute_decision(_decision(), kill_switch=False, shadow_mode=False)
        out = reconcile_resting_entries(
            ex,
            self.ledger,
            record_open=orch.record_filled_open,
            now=time.time() + 901,
            ttl_seconds=900,
        )
        self.assertEqual([r["outcome"] for r in out], ["canceled_stale"])
        self.assertEqual(ex.cancelled, ["ord-9"])
        self.assertEqual(self.ledger.get_trades(action="open"), [])
        self.assertEqual(len(self.ledger.get_events(event_type=ORDER_RESOLVED_EVENT)), 1)

    def test_missing_algo_after_fill_places_repair_oco(self) -> None:
        ex = _OkxLikeExchange()
        ex.order_rows = [_order_row("filled", fill=1, px=84000.0, fill_ms=1_790_000_000_000)]
        ex.algos = []
        self._orch(ex).execute_decision(_decision(), kill_switch=False, shadow_mode=False)
        self.assertEqual(ex.placed_oco, [(INST, "short", 84505.0, 82890.0)])
        meta = self.ledger.get_trades(action="open", limit=1)[0].metadata or {}
        self.assertTrue(meta.get("sl_tp_repaired"))
        self.assertEqual(len(self.ledger.get_events(event_type="sl_tp_repaired")), 1)
        self.assertEqual(len(self.ledger.get_events(event_type="sl_tp_attach_failed")), 0)


def _hist(*, c_ms: int, u_ms: int, open_px: float, close_px: float, pnl: float, realized: float, fee: float, side: str = "short") -> dict:
    return {
        "instId": INST,
        "posSide": side,
        "cTime": str(c_ms),
        "uTime": str(u_ms),
        "openAvgPx": str(open_px),
        "closeAvgPx": str(close_px),
        "closeTotalPos": "1",
        "pnl": str(pnl),
        "realizedPnl": str(realized),
        "fee": str(fee),
        "fundingFee": "0",
        "type": "2",
        "posId": "p1",
    }


class TestCloseReconcileTruth(_LedgerCase):
    def _open(self, *, ts: float, price: float, fill_ts: float, order_id: str) -> int:
        return self.ledger.record_trade(
            TradeRecord(
                timestamp=ts,
                inst_id=INST,
                action="open",
                direction="short",
                size=1.0,
                price=price,
                strategy_tag="keel-llm",
                metadata={"order_id": order_id, "fill_ts": fill_ts, "stop_loss": price + 500, "take_profit": price - 1100},
            )
        )

    def _pos(self) -> Position:
        return Position(inst_id=INST, side="short", size=1.0, avg_price=84000.0, mark_price=84000.0, leverage=5.0)

    def test_same_key_new_ids_closes_dropped_open(self) -> None:
        """#274→#277: old short SL'd and a new short opened between two snapshots."""
        now = time.time()
        a = self._open(ts=now - 3600, price=84100.0, fill_ts=now - 3600, order_id="A")
        ex = _OkxLikeExchange()
        ex.positions = [self._pos()]
        reconcile_closed_positions(ex, self.ledger, now=now - 600)  # baseline tracks A
        b = self._open(ts=now - 120, price=84000.0, fill_ts=now - 120, order_id="B")
        ex.history = [
            _hist(
                c_ms=int((now - 3600) * 1000),
                u_ms=int((now - 300) * 1000),
                open_px=84100.0,
                close_px=84057.0,
                pnl=0.43,
                realized=-0.17,
                fee=-0.60,
            )
        ]
        out = reconcile_closed_positions(ex, self.ledger, now=now)
        self.assertEqual([o.open_trade_id for o in out], [a])
        close = self.ledger.get_trades(action="close", limit=1)[0]
        self.assertAlmostEqual(close.price, 84057.0)
        self.assertAlmostEqual(close.pnl or 0, -0.17)  # net realized, not price diff
        self.assertAlmostEqual(close.fee, 0.60)
        self.assertAlmostEqual(close.timestamp, now - 300, places=2)  # OKX close time
        self.assertEqual((close.metadata or {}).get("source"), "okx_positions_history")
        seen = self.ledger.get_events(event_type=POSITIONS_SEEN_EVENT, limit=1)[0]
        self.assertEqual(seen.data["positions"][0]["open_trade_ids"], [b])

    def test_vanished_key_prefers_history_over_ticker(self) -> None:
        now = time.time()
        a = self._open(ts=now - 3600, price=84018.3, fill_ts=now - 3600, order_id="A")
        ex = _OkxLikeExchange()
        ex.positions = [self._pos()]
        reconcile_closed_positions(ex, self.ledger, now=now - 600)
        ex.positions = []
        ex.history = [
            _hist(
                c_ms=int((now - 3600) * 1000) + 800,
                u_ms=int((now - 400) * 1000),
                open_px=84018.3,
                close_px=84500.0,
                pnl=-4.817,
                realized=-5.4075,
                fee=-0.5905,
            )
        ]
        out = reconcile_closed_positions(ex, self.ledger, now=now)
        self.assertEqual([o.open_trade_id for o in out], [a])
        self.assertEqual(out[0].exit_reason, "sl")
        close = self.ledger.get_trades(action="close", limit=1)[0]
        self.assertAlmostEqual(close.price, 84500.0)  # ticker would say 84000
        self.assertAlmostEqual((close.metadata or {}).get("gross_pnl"), -4.817)

    def test_sweep_backfills_only_when_history_proves_close(self) -> None:
        now = time.time()
        orphan = self._open(ts=now - 7200, price=2678.86, fill_ts=now - 7200, order_id="O")
        ex = _OkxLikeExchange()
        # No history → nothing invented.
        self.assertEqual(reconcile_closed_positions(ex, self.ledger, now=now), [])
        self.assertEqual(self.ledger.get_trades(action="close"), [])
        ex.history = [
            _hist(
                c_ms=int((now - 7200) * 1000),
                u_ms=int((now - 3000) * 1000),
                open_px=2678.86,
                close_px=2677.52,
                pnl=0.134,
                realized=-0.231,
                fee=-0.187,
            )
        ]
        out = reconcile_closed_positions(ex, self.ledger, now=now)
        self.assertEqual([o.open_trade_id for o in out], [orphan])
        close = self.ledger.get_trades(action="close", limit=1)[0]
        self.assertEqual((close.metadata or {}).get("source"), "okx_positions_history_backfill")
        self.assertEqual(reconcile_closed_positions(ex, self.ledger, now=now + 60), [])

    def test_sweep_ignores_shadow_and_outside_lookback(self) -> None:
        now = time.time()
        self.ledger.record_trade(
            TradeRecord(
                timestamp=now - 7200, inst_id=INST, action="open", direction="short", size=1.0,
                price=100.0, strategy_tag="keel-shadow", metadata={"fill_ts": now - 7200, "shadow": True},
            )
        )
        self._open(ts=now - 4 * 86400, price=100.0, fill_ts=now - 4 * 86400, order_id="old")
        ex = _OkxLikeExchange()
        ex.history = [
            _hist(c_ms=int((now - 7200) * 1000), u_ms=int(now * 1000), open_px=100, close_px=99, pnl=1, realized=1, fee=0),
            _hist(c_ms=int((now - 4 * 86400) * 1000), u_ms=int(now * 1000), open_px=100, close_px=99, pnl=1, realized=1, fee=0),
        ]
        self.assertEqual(reconcile_closed_positions(ex, self.ledger, now=now), [])

    def test_match_history_tolerance(self) -> None:
        rows = [_hist(c_ms=1_000_000, u_ms=2_000_000, open_px=1, close_px=1, pnl=0, realized=0, fee=0)]
        self.assertIsNotNone(match_history_close(rows, inst_id=INST, side="short", fill_ts=1003.0))
        self.assertIsNone(match_history_close(rows, inst_id=INST, side="short", fill_ts=1010.0))
        self.assertIsNone(match_history_close(rows, inst_id=INST, side="long", fill_ts=1000.0))


class TestCycleOrdering(unittest.TestCase):
    def test_close_reconcile_runs_before_cooldown(self) -> None:
        """Post-exit cooldown must see closes detected this cycle (#278 bypass)."""
        from keel.exchange.paper import PaperAdapter
        from keel.worker import cycle as cyc

        calls: list[str] = []
        real_cd = cyc.apply_rule_fire_cooldown

        def rec_reconcile(*a, **k):
            calls.append("reconcile")
            return []

        def rec_cd(decision, **k):
            calls.append("cooldown")
            return real_cd(decision, **k)

        with tempfile.TemporaryDirectory() as tmp:
            ledger = KeelLedger(Path(tmp) / "c.db")
            try:
                with patch.object(cyc, "reconcile_closed_positions", rec_reconcile), patch.object(
                    cyc, "apply_rule_fire_cooldown", rec_cd
                ):
                    cyc.run_paper_cycle(
                        exchange=PaperAdapter(initial_balance=10_000.0),
                        ledger=ledger,
                        instrument_ids=[INST],
                        force_paper=True,
                    )
            finally:
                ledger.close()
        self.assertIn("cooldown", calls)
        self.assertEqual(calls[0], "reconcile")


class TestKnobs(unittest.TestCase):
    def test_llm_demo_profile_defaults(self) -> None:
        prof = profile_defaults("llm_demo")
        self.assertEqual(prof["KEEL_LLM_ADX_MIN"], "20")
        self.assertEqual(prof["KEEL_LLM_BE_R"], "1.0")
        self.assertEqual(prof["KEEL_ENTRY_TTL_SECONDS"], "900")
        self.assertEqual(prof["KEEL_CLOSE_BACKFILL_LOOKBACK_HOURS"], "72")
        # Guardrails unchanged.
        self.assertEqual(prof["KEEL_DECISION_POLICY"], "llm")
        self.assertEqual(prof["KEEL_KILL_SWITCH"], "0")
        self.assertEqual(prof["KEEL_SHADOW_MODE"], "0")

    def test_be_r_one_waits_for_full_r(self) -> None:
        kw = dict(side="short", entry=84000.0, sl=84500.0, tp=82900.0, atr=200.0)
        with patch.dict(os.environ, {"KEEL_LLM_BE_R": "1.0"}):
            self.assertEqual(be_trigger_r(), 1.0)
            early = plan_protect(mark=83700.0, **kw)  # +0.6R
            self.assertFalse(bool(early and early.breakeven))
            late = plan_protect(mark=83450.0, **kw)  # +1.1R
            self.assertTrue(late is not None and late.breakeven)
            self.assertLess(late.new_sl, 84000.0)


class TestOkxAdapterExtras(unittest.TestCase):
    def _adapter(self, responses: dict[str, str]) -> tuple[OKXRestAdapter, list]:
        calls: list = []

        def transport(method, url, headers, body):
            calls.append((method, url, body))
            for key, payload in responses.items():
                if key in url:
                    return payload
            return json.dumps({"code": "0", "data": []})

        ad = OKXRestAdapter("k", "s", "p", demo=True, transport=transport)
        ad._account_cfg = {"posMode": "long_short_mode", "acctLv": "2"}
        return ad, calls

    def test_get_order_and_positions_history(self) -> None:
        ad, calls = self._adapter(
            {
                "/trade/order?": json.dumps({"code": "0", "data": [{"state": "filled", "avgPx": "1"}]}),
                "/account/positions-history": json.dumps({"code": "0", "data": [{"instId": INST}]}),
            }
        )
        self.assertEqual((ad.get_order(INST, "42") or {}).get("state"), "filled")
        self.assertIn("ordId=42", calls[-1][1])
        self.assertEqual(ad.get_positions_history()[0]["instId"], INST)

    def test_place_oco_tpsl_full_position(self) -> None:
        ad, calls = self._adapter(
            {"/trade/order-algo": json.dumps({"code": "0", "data": [{"algoId": "77", "sCode": "0"}]})}
        )
        self.assertEqual(ad.place_oco_tpsl(INST, "short", sl=84505.0, tp=82890.0), "77")
        body = json.loads(calls[-1][2])
        self.assertEqual(body["side"], "buy")
        self.assertEqual(body["posSide"], "short")
        self.assertEqual(body["ordType"], "oco")
        self.assertEqual(body["closeFraction"], "1")
        self.assertEqual(body["slOrdPx"], "-1")


if __name__ == "__main__":
    unittest.main()
