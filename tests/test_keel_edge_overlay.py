"""LLM-as-trader kernel overlay: HTF gate, RSI chase, 2.2/1.0 ATR geometry."""
from __future__ import annotations

import os
import unittest

from keel.domain.decision import Decision
from keel.factors.market_data import MarketSnapshot
from keel.policy.edge_overlay import (
    BOOK_GATE,
    GEOMETRY_GATE,
    HTF_GATE,
    RSI_CHASE_GATE,
    apply_llm_book_lock,
    apply_llm_edge_overlay,
    plan_llm_geometry,
)

_ENV_KEYS = (
    "KEEL_LLM_EDGE_OVERLAY",
    "KEEL_LLM_REQUIRE_1H",
    "KEEL_LLM_REQUIRE_4H",
    "KEEL_LLM_SL_ATR",
    "KEEL_LLM_TP_RR",
    "KEEL_LLM_RT_FEE_BPS",
    "KEEL_LLM_FEE_SL_MULT",
    "KEEL_LLM_FEE_TP_MULT",
    "KEEL_LLM_NO_SCALE_IN",
    "KEEL_LLM_REENTRY_SECONDS",
)


def _snap(**kwargs: object) -> MarketSnapshot:
    fields: dict[str, object] = {
        "inst_id": "BTC-USDT-SWAP",
        "name": "BTC",
        "timestamp": 1.0,
        "price": 77359.2,
        "atr_14": 200.0,
        "rsi_14": 55.0,
        "trend_15m": "bullish",
        "trend_1h": "neutral",
        "trend_4h": "neutral",
        "data_valid": True,
    }
    fields.update(kwargs)
    return MarketSnapshot(**fields)  # type: ignore[arg-type]


def _buy(inst_id: str = "BTC-USDT-SWAP", **kwargs: object) -> Decision:
    fields: dict[str, object] = {
        "inst_id": inst_id,
        "action": "BUY_LONG",
        "confidence": 70.0,
        "entry_price": 77359.2,
        "take_profit": 77613.0,
        "stop_loss": 77257.0,
        "leverage": 5,
        "margin_usdt": 50.0,
        "reason": "llm long",
    }
    fields.update(kwargs)
    return Decision(**fields)  # type: ignore[arg-type]


class TestLlmEdgeOverlay(unittest.TestCase):
    def setUp(self) -> None:
        self._saved = {k: os.environ.get(k) for k in _ENV_KEYS}
        for k in _ENV_KEYS:
            os.environ.pop(k, None)

    def tearDown(self) -> None:
        for k in _ENV_KEYS:
            os.environ.pop(k, None)
        for k, v in self._saved.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v

    def test_btc_like_15m_only_waits(self) -> None:
        out = apply_llm_edge_overlay(_buy(), _snap())
        self.assertEqual(out.action, "WAIT")
        self.assertIn(HTF_GATE, (out.signal_diag or {}).get("missing") or [])
        self.assertFalse((out.signal_diag or {}).get(HTF_GATE))
        self.assertEqual((out.signal_diag or {}).get("nearest"), "none")

    def test_eth_like_all_tf_rewrites_geometry(self) -> None:
        entry = 2533.0
        atr = 20.0
        snap = _snap(
            inst_id="ETH-USDT-SWAP",
            name="ETH",
            price=entry,
            atr_14=atr,
            rsi_14=55.0,
            trend_15m="bullish",
            trend_1h="bullish",
            trend_4h="bullish",
        )
        raw = _buy(
            inst_id="ETH-USDT-SWAP",
            entry_price=entry,
            take_profit=2560.0,
            stop_loss=2520.0,
        )
        out = apply_llm_edge_overlay(raw, snap)
        self.assertEqual(out.action, "BUY_LONG")
        plan = plan_llm_geometry(entry, atr)
        self.assertAlmostEqual(out.stop_loss or 0.0, entry - plan["sl_dist"])
        self.assertAlmostEqual(out.take_profit or 0.0, entry + plan["tp_dist"])
        diag = out.signal_diag or {}
        self.assertTrue(diag.get(HTF_GATE))
        self.assertTrue(diag.get(RSI_CHASE_GATE))
        self.assertTrue(diag.get(GEOMETRY_GATE))
        self.assertGreaterEqual(plan["tp_dist"] / plan["sl_dist"], 2.2 - 1e-9)
        # ATR=20 dominates the 10bp×6 fee floor on ETH (~15px).
        self.assertAlmostEqual(plan["sl_dist"], 2.0 * atr)

    def test_rsi_chase_long_waits(self) -> None:
        snap = _snap(
            rsi_14=75.0,
            trend_1h="bullish",
            trend_4h="bullish",
        )
        out = apply_llm_edge_overlay(_buy(), snap)
        self.assertEqual(out.action, "WAIT")
        self.assertIn(RSI_CHASE_GATE, (out.signal_diag or {}).get("missing") or [])
        self.assertFalse((out.signal_diag or {}).get(RSI_CHASE_GATE))

    def test_overlay_off_keeps_llm_geometry(self) -> None:
        os.environ["KEEL_LLM_EDGE_OVERLAY"] = "0"
        raw = _buy()
        out = apply_llm_edge_overlay(raw, _snap())
        self.assertEqual(out.action, "BUY_LONG")
        self.assertEqual(out.stop_loss, raw.stop_loss)
        self.assertEqual(out.take_profit, raw.take_profit)

    def test_wait_passthrough(self) -> None:
        wait = Decision(inst_id="BTC-USDT-SWAP", action="WAIT", reason="model wait")
        out = apply_llm_edge_overlay(wait, _snap(trend_1h="bullish", trend_4h="bullish"))
        self.assertIs(out, wait)

    def test_require_4h_off_allows_1h_only(self) -> None:
        os.environ["KEEL_LLM_REQUIRE_4H"] = "0"
        snap = _snap(trend_1h="bullish", trend_4h="neutral")
        out = apply_llm_edge_overlay(_buy(), snap)
        self.assertEqual(out.action, "BUY_LONG")
        self.assertTrue((out.signal_diag or {}).get(HTF_GATE))

    def test_fee_floor_dominates_small_btc_atr(self) -> None:
        entry = 77000.0
        atr = 60.0
        plan = plan_llm_geometry(entry, atr)
        fee_sl = entry * 0.0010 * 6.0
        self.assertGreater(fee_sl, 2.0 * atr)
        self.assertAlmostEqual(plan["sl_dist"], fee_sl)
        self.assertGreaterEqual(plan["tp_dist"], 2.2 * plan["sl_dist"] - 1e-9)
        self.assertGreaterEqual(plan["tp_dist"], entry * 0.0010 * 12.0)

    def test_book_lock_blocks_scale_in_and_hedge(self) -> None:
        from keel.exchange.protocol import Position

        long_pos = Position(
            inst_id="BTC-USDT-SWAP",
            side="long",
            size=1.0,
            avg_price=77000.0,
            mark_price=77010.0,
            leverage=5.0,
        )
        buy = apply_llm_book_lock(_buy(), [long_pos])
        self.assertEqual(buy.action, "WAIT")
        self.assertIn(BOOK_GATE, (buy.signal_diag or {}).get("missing") or [])
        short = Decision(
            inst_id="BTC-USDT-SWAP",
            action="SELL_SHORT",
            confidence=70.0,
            entry_price=77000.0,
            take_profit=76000.0,
            stop_loss=77500.0,
            reason="hedge",
        )
        out = apply_llm_book_lock(short, [long_pos])
        self.assertEqual(out.action, "WAIT")
        self.assertIn("hedge", out.reason.lower())

    def test_book_lock_allows_flat(self) -> None:
        out = apply_llm_book_lock(_buy(), [])
        self.assertEqual(out.action, "BUY_LONG")


if __name__ == "__main__":
    unittest.main()
