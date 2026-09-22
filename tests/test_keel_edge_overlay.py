"""LLM-as-trader kernel overlay: HTF, soft4h+15m, ADX, RSI mid/chase, confidence, geometry."""
from __future__ import annotations

import os
import unittest

from keel.domain.decision import Decision
from keel.factors.market_data import MarketSnapshot
from keel.policy.edge_overlay import (
    ADX_GATE,
    BOOK_GATE,
    CONFIDENCE_GATE,
    GEOMETRY_GATE,
    HTF_GATE,
    RSI_CHASE_GATE,
    RSI_MID_GATE,
    SOFT4H_15M_GATE,
    TF15_GATE,
    apply_llm_book_lock,
    apply_llm_edge_overlay,
    plan_llm_geometry,
)

_ENV_KEYS = (
    "KEEL_LLM_EDGE_OVERLAY",
    "KEEL_LLM_REQUIRE_1H",
    "KEEL_LLM_REQUIRE_4H",
    "KEEL_LLM_4H_MODE",
    "KEEL_LLM_REQUIRE_15M_ALIGN",
    "KEEL_LLM_SOFT4H_BLOCK_15M_NEUTRAL",
    "KEEL_LLM_ADX_MIN",
    "KEEL_LLM_ADX_PERIOD",
    "KEEL_LLM_SHORT_RSI_MAX",
    "KEEL_LLM_LONG_RSI_MIN",
    "KEEL_LLM_RSI_CHASE_LONG_MAX",
    "KEEL_LLM_RSI_CHASE_SHORT_MIN",
    "KEEL_LLM_MIN_CONFIDENCE",
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


def _sell(inst_id: str = "BTC-USDT-SWAP", **kwargs: object) -> Decision:
    fields: dict[str, object] = {
        "inst_id": inst_id,
        "action": "SELL_SHORT",
        "confidence": 75.0,
        "entry_price": 77359.2,
        "take_profit": 76000.0,
        "stop_loss": 78000.0,
        "leverage": 5,
        "margin_usdt": 50.0,
        "reason": "llm short",
    }
    fields.update(kwargs)
    return Decision(**fields)  # type: ignore[arg-type]


def _aligned_long_snap(**kwargs: object) -> MarketSnapshot:
    base = dict(
        rsi_14=55.0,
        trend_15m="bullish",
        trend_1h="bullish",
        trend_4h="bullish",
    )
    base.update(kwargs)
    return _snap(**base)


def _aligned_short_snap(**kwargs: object) -> MarketSnapshot:
    base = dict(
        rsi_14=42.0,
        trend_15m="bearish",
        trend_1h="bearish",
        trend_4h="bearish",
    )
    base.update(kwargs)
    return _snap(**base)


class TestLlmEdgeOverlay(unittest.TestCase):
    def setUp(self) -> None:
        self._saved = {k: os.environ.get(k) for k in _ENV_KEYS}
        for k in _ENV_KEYS:
            os.environ.pop(k, None)
        # Deterministic F7 defaults for unit tests (ADX off → no candle dependency).
        os.environ["KEEL_LLM_ADX_MIN"] = "0"
        # Pin P1-2 off so profile llm_demo soft4h_block cannot leak via _DOTENV_VALUES.
        os.environ["KEEL_LLM_SOFT4H_BLOCK_15M_NEUTRAL"] = "0"

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
        snap = _aligned_long_snap(
            inst_id="ETH-USDT-SWAP",
            name="ETH",
            price=entry,
            atr_14=atr,
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
        self.assertTrue(diag.get(TF15_GATE))
        self.assertTrue(diag.get(ADX_GATE))
        self.assertTrue(diag.get(RSI_CHASE_GATE))
        self.assertTrue(diag.get(RSI_MID_GATE))
        self.assertTrue(diag.get(CONFIDENCE_GATE))
        self.assertTrue(diag.get(GEOMETRY_GATE))
        self.assertGreaterEqual(plan["tp_dist"] / plan["sl_dist"], 2.2 - 1e-9)
        self.assertAlmostEqual(plan["sl_dist"], 2.0 * atr)

    def test_rsi_chase_long_waits(self) -> None:
        snap = _aligned_long_snap(rsi_14=75.0)
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
        out = apply_llm_edge_overlay(wait, _aligned_long_snap())
        self.assertIs(out, wait)

    def test_require_4h_off_allows_1h_only(self) -> None:
        os.environ["KEEL_LLM_REQUIRE_4H"] = "0"
        snap = _aligned_long_snap(trend_4h="neutral")
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

    # --- F7 gates ---

    def test_f7_15m_opposing_blocks_short(self) -> None:
        # HTF bearish OK, but 15m bullish opposes short.
        snap = _aligned_short_snap(trend_15m="bullish", rsi_14=40.0)
        out = apply_llm_edge_overlay(_sell(), snap)
        self.assertEqual(out.action, "WAIT")
        self.assertIn(TF15_GATE, (out.signal_diag or {}).get("missing") or [])
        self.assertFalse((out.signal_diag or {}).get(TF15_GATE))

    def test_f7_15m_opposing_blocks_long(self) -> None:
        snap = _aligned_long_snap(trend_15m="bearish", rsi_14=55.0)
        out = apply_llm_edge_overlay(_buy(), snap)
        self.assertEqual(out.action, "WAIT")
        self.assertIn(TF15_GATE, (out.signal_diag or {}).get("missing") or [])

    def test_f7_15m_neutral_allows_fire(self) -> None:
        snap = _aligned_short_snap(trend_15m="neutral", rsi_14=40.0)
        out = apply_llm_edge_overlay(_sell(), snap)
        self.assertEqual(out.action, "SELL_SHORT")
        self.assertTrue((out.signal_diag or {}).get(TF15_GATE))

    def test_f7_15m_align_off(self) -> None:
        os.environ["KEEL_LLM_REQUIRE_15M_ALIGN"] = "0"
        snap = _aligned_short_snap(trend_15m="bullish", rsi_14=40.0)
        out = apply_llm_edge_overlay(_sell(), snap)
        self.assertEqual(out.action, "SELL_SHORT")

    def test_f7_rsi_mid_blocks_short(self) -> None:
        # Pin pre-F8 band: RSI 50 is mid-range for shorts when max=48.
        os.environ["KEEL_LLM_SHORT_RSI_MAX"] = "48"
        snap = _aligned_short_snap(rsi_14=50.0)
        out = apply_llm_edge_overlay(_sell(), snap)
        self.assertEqual(out.action, "WAIT")
        self.assertIn(RSI_MID_GATE, (out.signal_diag or {}).get("missing") or [])
        self.assertFalse((out.signal_diag or {}).get(RSI_MID_GATE))

    def test_f7_rsi_mid_blocks_long(self) -> None:
        os.environ["KEEL_LLM_LONG_RSI_MIN"] = "52"
        snap = _aligned_long_snap(rsi_14=50.0)
        out = apply_llm_edge_overlay(_buy(), snap)
        self.assertEqual(out.action, "WAIT")
        self.assertIn(RSI_MID_GATE, (out.signal_diag or {}).get("missing") or [])

    def test_f7_min_confidence_blocks(self) -> None:
        os.environ["KEEL_LLM_MIN_CONFIDENCE"] = "70"
        snap = _aligned_long_snap(rsi_14=55.0)
        out = apply_llm_edge_overlay(_buy(confidence=65.0), snap)
        self.assertEqual(out.action, "WAIT")
        self.assertIn(CONFIDENCE_GATE, (out.signal_diag or {}).get("missing") or [])
        self.assertFalse((out.signal_diag or {}).get(CONFIDENCE_GATE))

    def test_f7_adx_floor_blocks_when_present(self) -> None:
        os.environ["KEEL_LLM_ADX_MIN"] = "18"
        snap = _aligned_long_snap(rsi_14=55.0)
        # Inject snapshot ADX below floor (attr may be absent on MarketSnapshot).
        snap.adx_14 = 12.0  # type: ignore[attr-defined]
        out = apply_llm_edge_overlay(_buy(), snap)
        self.assertEqual(out.action, "WAIT")
        self.assertIn(ADX_GATE, (out.signal_diag or {}).get("missing") or [])
        self.assertEqual((out.signal_diag or {}).get("adx_source"), "snapshot")

    def test_f7_adx_fail_open_when_unavailable(self) -> None:
        os.environ["KEEL_LLM_ADX_MIN"] = "18"
        snap = _aligned_long_snap(rsi_14=55.0)  # no candles, no adx attr
        out = apply_llm_edge_overlay(_buy(), snap)
        self.assertEqual(out.action, "BUY_LONG")
        diag = out.signal_diag or {}
        self.assertTrue(diag.get(ADX_GATE))
        self.assertTrue(diag.get("adx_fail_open"))
        self.assertEqual(diag.get("adx_source"), "unavailable")

    def test_f7_adx_pass_when_above_floor(self) -> None:
        os.environ["KEEL_LLM_ADX_MIN"] = "18"
        snap = _aligned_long_snap(rsi_14=55.0)
        snap.adx_14 = 22.0  # type: ignore[attr-defined]
        out = apply_llm_edge_overlay(_buy(), snap)
        self.assertEqual(out.action, "BUY_LONG")
        diag = out.signal_diag or {}
        self.assertTrue(diag.get(ADX_GATE))
        self.assertFalse(diag.get("adx_fail_open"))
        self.assertEqual(diag.get("adx"), 22.0)

    # --- F8 soft-4h + relaxed defaults ---

    def test_f8_soft_4h_neutral_allows_1h_align_long(self) -> None:
        """Market-like: 4h neutral, 1h bullish → fire under soft (default)."""
        snap = _aligned_long_snap(trend_4h="neutral", rsi_14=55.0)
        out = apply_llm_edge_overlay(_buy(confidence=65.0), snap)
        self.assertEqual(out.action, "BUY_LONG")
        diag = out.signal_diag or {}
        self.assertTrue(diag.get(HTF_GATE))
        self.assertEqual(diag.get("llm_4h_mode"), "soft")
        self.assertEqual(diag.get("trend_4h"), "neutral")

    def test_f8_soft_4h_neutral_allows_1h_align_short(self) -> None:
        snap = _aligned_short_snap(trend_4h="neutral", rsi_14=40.0)
        out = apply_llm_edge_overlay(_sell(confidence=65.0), snap)
        self.assertEqual(out.action, "SELL_SHORT")
        self.assertTrue((out.signal_diag or {}).get(HTF_GATE))
        self.assertEqual((out.signal_diag or {}).get("llm_4h_mode"), "soft")

    def test_f8_soft_4h_opposing_still_blocks(self) -> None:
        snap = _aligned_long_snap(trend_4h="bearish", rsi_14=55.0)
        out = apply_llm_edge_overlay(_buy(), snap)
        self.assertEqual(out.action, "WAIT")
        self.assertIn(HTF_GATE, (out.signal_diag or {}).get("missing") or [])
        self.assertEqual((out.signal_diag or {}).get("llm_4h_mode"), "soft")

    def test_f8_hard_4h_neutral_blocks(self) -> None:
        os.environ["KEEL_LLM_4H_MODE"] = "hard"
        snap = _aligned_long_snap(trend_4h="neutral", rsi_14=55.0)
        out = apply_llm_edge_overlay(_buy(), snap)
        self.assertEqual(out.action, "WAIT")
        self.assertIn(HTF_GATE, (out.signal_diag or {}).get("missing") or [])
        self.assertEqual((out.signal_diag or {}).get("llm_4h_mode"), "hard")

    def test_f8_relaxed_rsi_mid_allows_50(self) -> None:
        """F8 defaults: short_max=52 / long_min=48 → RSI 50 clears mid veto."""
        long_out = apply_llm_edge_overlay(_buy(confidence=65.0), _aligned_long_snap(rsi_14=50.0))
        self.assertEqual(long_out.action, "BUY_LONG")
        short_out = apply_llm_edge_overlay(
            _sell(confidence=65.0), _aligned_short_snap(rsi_14=50.0)
        )
        self.assertEqual(short_out.action, "SELL_SHORT")

    def test_f8_relaxed_confidence_allows_65(self) -> None:
        snap = _aligned_long_snap(rsi_14=55.0)
        out = apply_llm_edge_overlay(_buy(confidence=65.0), snap)
        self.assertEqual(out.action, "BUY_LONG")
        self.assertTrue((out.signal_diag or {}).get(CONFIDENCE_GATE))

    def test_f8_relaxed_adx_floor_15(self) -> None:
        # Leave ADX_MIN unset → F8 default 15; 16 passes, 12 blocks.
        os.environ.pop("KEEL_LLM_ADX_MIN", None)
        snap = _aligned_long_snap(rsi_14=55.0)
        snap.adx_14 = 16.0  # type: ignore[attr-defined]
        out = apply_llm_edge_overlay(_buy(confidence=65.0), snap)
        self.assertEqual(out.action, "BUY_LONG")
        snap2 = _aligned_long_snap(rsi_14=55.0)
        snap2.adx_14 = 12.0  # type: ignore[attr-defined]
        out2 = apply_llm_edge_overlay(_buy(confidence=65.0), snap2)
        self.assertEqual(out2.action, "WAIT")
        self.assertIn(ADX_GATE, (out2.signal_diag or {}).get("missing") or [])

    # --- F10 soft-4h needs 15m same-dir + tighter chase ---

    def test_f10_soft4h_15m_neutral_waits(self) -> None:
        """soft + 4h neutral + 15m neutral → WAIT soft4h_needs_15m."""
        snap = _aligned_long_snap(trend_4h="neutral", trend_15m="neutral", rsi_14=55.0)
        out = apply_llm_edge_overlay(_buy(confidence=70.0), snap)
        self.assertEqual(out.action, "WAIT")
        diag = out.signal_diag or {}
        self.assertIn(SOFT4H_15M_GATE, diag.get("missing") or [])
        self.assertFalse(diag.get(SOFT4H_15M_GATE))
        self.assertTrue(diag.get("soft4h_15m_applies"))
        self.assertIn("soft4h needs 15m", out.reason)

    def test_f10_soft4h_15m_opposing_waits(self) -> None:
        snap = _aligned_short_snap(trend_4h="neutral", trend_15m="bullish", rsi_14=40.0)
        out = apply_llm_edge_overlay(_sell(confidence=70.0), snap)
        self.assertEqual(out.action, "WAIT")
        self.assertIn(SOFT4H_15M_GATE, (out.signal_diag or {}).get("missing") or [])

    def test_f10_soft4h_15m_align_passes(self) -> None:
        """soft + 4h neutral + 15m same-dir → HTF/15m combo passes."""
        snap = _aligned_long_snap(trend_4h="neutral", trend_15m="bullish", rsi_14=55.0)
        out = apply_llm_edge_overlay(_buy(confidence=70.0), snap)
        self.assertEqual(out.action, "BUY_LONG")
        diag = out.signal_diag or {}
        self.assertTrue(diag.get(HTF_GATE))
        self.assertTrue(diag.get(SOFT4H_15M_GATE))
        self.assertTrue(diag.get(TF15_GATE))
        self.assertTrue(diag.get("soft4h_15m_applies"))
        self.assertEqual(diag.get("llm_4h_mode"), "soft")

    def test_f10_soft4h_15m_align_short_passes(self) -> None:
        snap = _aligned_short_snap(trend_4h="neutral", trend_15m="bearish", rsi_14=40.0)
        out = apply_llm_edge_overlay(_sell(confidence=70.0), snap)
        self.assertEqual(out.action, "SELL_SHORT")
        diag = out.signal_diag or {}
        self.assertTrue(diag.get(SOFT4H_15M_GATE))
        self.assertTrue(diag.get("soft4h_15m_applies"))

    def test_f10_aligned_4h_allows_15m_neutral(self) -> None:
        """When 4h already same-dir, soft4h confirm N/A; 15m neutral still OK (F7)."""
        snap = _aligned_long_snap(trend_4h="bullish", trend_15m="neutral", rsi_14=55.0)
        out = apply_llm_edge_overlay(_buy(confidence=70.0), snap)
        self.assertEqual(out.action, "BUY_LONG")
        diag = out.signal_diag or {}
        self.assertTrue(diag.get(SOFT4H_15M_GATE))
        self.assertFalse(diag.get("soft4h_15m_applies"))

    def test_f10_rsi_chase_long_67_waits(self) -> None:
        """F10 chase long max 65 — RSI 67 (under old 70) now WAIT."""
        snap = _aligned_long_snap(rsi_14=67.0)
        out = apply_llm_edge_overlay(_buy(confidence=70.0), snap)
        self.assertEqual(out.action, "WAIT")
        self.assertIn(RSI_CHASE_GATE, (out.signal_diag or {}).get("missing") or [])
        self.assertEqual((out.signal_diag or {}).get("rsi_chase_long_max"), 65.0)

    def test_f10_rsi_chase_short_33_waits(self) -> None:
        snap = _aligned_short_snap(rsi_14=33.0)
        out = apply_llm_edge_overlay(_sell(confidence=70.0), snap)
        self.assertEqual(out.action, "WAIT")
        self.assertIn(RSI_CHASE_GATE, (out.signal_diag or {}).get("missing") or [])
        self.assertEqual((out.signal_diag or {}).get("rsi_chase_short_min"), 35.0)

    def test_f10_rsi_chase_long_65_allows(self) -> None:
        snap = _aligned_long_snap(rsi_14=65.0)
        out = apply_llm_edge_overlay(_buy(confidence=70.0), snap)
        self.assertEqual(out.action, "BUY_LONG")
        self.assertTrue((out.signal_diag or {}).get(RSI_CHASE_GATE))


    def test_p1_block_15m_neutral_when_4h_aligned(self) -> None:
        """P1-2: KEEL_LLM_SOFT4H_BLOCK_15M_NEUTRAL=1 → 15m neutral WAIT even if 4h aligned."""
        os.environ["KEEL_LLM_SOFT4H_BLOCK_15M_NEUTRAL"] = "1"
        snap = _aligned_long_snap(trend_4h="bullish", trend_15m="neutral", rsi_14=55.0)
        out = apply_llm_edge_overlay(_buy(confidence=70.0), snap)
        self.assertEqual(out.action, "WAIT")
        diag = out.signal_diag or {}
        self.assertIn(TF15_GATE, diag.get("missing") or [])
        self.assertFalse(diag.get(TF15_GATE))
        self.assertTrue(diag.get("block_15m_neutral"))
        self.assertIn("15m same-dir", out.reason)

    def test_p1_block_15m_neutral_allows_same_dir(self) -> None:
        os.environ["KEEL_LLM_SOFT4H_BLOCK_15M_NEUTRAL"] = "1"
        snap = _aligned_long_snap(trend_4h="bullish", trend_15m="bullish", rsi_14=55.0)
        out = apply_llm_edge_overlay(_buy(confidence=70.0), snap)
        self.assertEqual(out.action, "BUY_LONG")
        diag = out.signal_diag or {}
        self.assertTrue(diag.get(TF15_GATE))
        self.assertTrue(diag.get("block_15m_neutral"))

    def test_p1_block_15m_neutral_off_keeps_f7(self) -> None:
        """Code default / explicit 0: 4h-aligned + 15m neutral still fires (F7)."""
        os.environ["KEEL_LLM_SOFT4H_BLOCK_15M_NEUTRAL"] = "0"
        snap = _aligned_long_snap(trend_4h="bullish", trend_15m="neutral", rsi_14=55.0)
        out = apply_llm_edge_overlay(_buy(confidence=70.0), snap)
        self.assertEqual(out.action, "BUY_LONG")
        self.assertFalse((out.signal_diag or {}).get("block_15m_neutral"))


if __name__ == "__main__":
    unittest.main()
