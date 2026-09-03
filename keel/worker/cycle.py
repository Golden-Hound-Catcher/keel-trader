"""
Paper/demo vertical trading cycle for Keel Trader.

Pipeline: factors → decision → risk → execution → ledger.

Default path uses PaperAdapter (no shell OKX CLI, no live exchange).
Legacy scripts/ai_factor_trader.py shims into this module.
"""
from __future__ import annotations

import argparse
import math
import os
import sys
import time
from pathlib import Path
from typing import Any

from keel.config import get_settings
from keel.domain.instruments import DEFAULT_CRYPTO_INSTRUMENTS, InstrumentPool
from keel.exchange.paper import PaperAdapter
from keel.exchange.protocol import Ticker
from keel.execution.orchestrator import ExecutionOrchestrator, ExecutionResult
from keel.factors.market_data import Candle, MarketSnapshot
from keel.factors.technical import (
    calculate_atr,
    calculate_bollinger,
    calculate_ema,
    calculate_macd,
    calculate_obv,
    calculate_rsi,
    calculate_vwap,
    classify_trend,
)
from keel.ledger import DecisionRecord, KeelLedger
from keel.llm.client import Decision


DEFAULT_SEED_PRICES: dict[str, float] = {
    "BTC-USDT-SWAP": 65000.0,
    "ETH-USDT-SWAP": 3200.0,
    "SOL-USDT-SWAP": 145.0,
    "DOGE-USDT-SWAP": 0.12,
    "SUI-USDT-SWAP": 1.8,
    "LINK-USDT-SWAP": 14.5,
}


def build_synthetic_candles(
    base_price: float,
    *,
    count: int = 64,
    drift: float = 0.0004,
    volatility: float = 0.008,
    now: float | None = None,
) -> list[Candle]:
    """Build deterministic synthetic OHLCV history (oldest → newest)."""
    stamp = now if now is not None else time.time()
    candles: list[Candle] = []
    price = base_price * 0.97
    for i in range(count):
        # Smooth sine + mild drift so RSI/MACD are non-flat but deterministic.
        wave = math.sin(i / 5.0) * volatility
        open_px = price
        close_px = price * (1.0 + drift + wave)
        high_px = max(open_px, close_px) * (1.0 + volatility * 0.35)
        low_px = min(open_px, close_px) * (1.0 - volatility * 0.35)
        volume = 1000.0 + (i % 7) * 50.0
        candles.append(
            Candle(
                timestamp=stamp - (count - i) * 900,
                open=open_px,
                high=high_px,
                low=low_px,
                close=close_px,
                volume=volume,
            )
        )
        price = close_px
    return candles


def enrich_snapshot(snapshot: MarketSnapshot) -> MarketSnapshot:
    """Compute technical factors onto a snapshot (pure math over candles)."""
    # MarketSnapshot stores newest-first in comments elsewhere; we keep oldest→newest.
    closes = [c.close for c in snapshot.candles_15m]
    highs = [c.high for c in snapshot.candles_15m]
    lows = [c.low for c in snapshot.candles_15m]
    volumes = [c.volume for c in snapshot.candles_15m]

    if len(closes) < 20:
        snapshot.data_valid = False
        snapshot.data_quality_reason = "insufficient candles"
        return snapshot

    ema_9 = calculate_ema(closes, 9)
    ema_21 = calculate_ema(closes, 21)
    ema_55 = calculate_ema(closes, 55)
    rsi_14 = calculate_rsi(closes, 14)
    rsi_7 = calculate_rsi(closes, 7)
    atr = calculate_atr(highs, lows, closes, 14)
    macd = calculate_macd(closes)
    bb = calculate_bollinger(closes)
    vwap = calculate_vwap(closes, volumes)
    obv = calculate_obv(closes, volumes)
    price = closes[-1]
    atr_pct = (atr / price * 100.0) if price else 0.0
    vwap_bias = ((price - vwap) / vwap * 100.0) if vwap else 0.0
    avg_vol = sum(volumes[-20:]) / 20.0 if volumes else 1.0
    vol_ratio = (volumes[-1] / avg_vol) if avg_vol else 1.0
    trend = classify_trend(ema_9, ema_21, ema_55, price)

    snapshot.price = price
    snapshot.ema_9 = ema_9
    snapshot.ema_21 = ema_21
    snapshot.ema_55 = ema_55
    snapshot.rsi_14 = rsi_14
    snapshot.rsi_7 = rsi_7
    snapshot.atr_14 = atr
    snapshot.atr_pct = atr_pct
    snapshot.macd_line = macd.macd_line
    snapshot.macd_signal = macd.signal_line
    snapshot.macd_histogram = macd.histogram
    snapshot.vwap = vwap
    snapshot.vwap_bias_pct = vwap_bias
    snapshot.obv = obv
    snapshot.volume_ratio = vol_ratio
    snapshot.trend_15m = trend  # type: ignore[assignment]
    snapshot.trend_1h = trend  # type: ignore[assignment]
    snapshot.trend_4h = trend  # type: ignore[assignment]
    snapshot.data_valid = True
    snapshot.data_quality_reason = "ok"
    # Silence unused local for lint-friendly completeness
    _ = bb
    return snapshot


def rule_based_decision(snapshot: MarketSnapshot) -> Decision:
    """
    Deterministic paper decision (no LLM).

    Produces valid RR >= 2 geometry when a signal fires so the risk/execution
    path is exercised end-to-end.
    """
    if not snapshot.data_valid or snapshot.price <= 0 or snapshot.atr_14 <= 0:
        return Decision(inst_id=snapshot.inst_id, action="WAIT", reason="invalid market data")

    price = snapshot.price
    atr = snapshot.atr_14
    margin = 50.0

    # Mild oversold + bullish structure → long
    if snapshot.rsi_14 <= 42 and snapshot.trend_15m == "bullish" and snapshot.macd_histogram >= 0:
        entry = price
        sl = entry - 1.0 * atr
        tp = entry + 2.2 * atr
        return Decision(
            inst_id=snapshot.inst_id,
            action="BUY_LONG",
            confidence=70.0,
            entry_price=entry,
            take_profit=tp,
            stop_loss=sl,
            leverage=3,
            margin_usdt=margin,
            reason=f"paper long rsi={snapshot.rsi_14:.1f} trend={snapshot.trend_15m}",
        )

    # Mild overbought + bearish structure → short
    if snapshot.rsi_14 >= 58 and snapshot.trend_15m == "bearish" and snapshot.macd_histogram <= 0:
        entry = price
        sl = entry + 1.0 * atr
        tp = entry - 2.2 * atr
        return Decision(
            inst_id=snapshot.inst_id,
            action="SELL_SHORT",
            confidence=70.0,
            entry_price=entry,
            take_profit=tp,
            stop_loss=sl,
            leverage=3,
            margin_usdt=margin,
            reason=f"paper short rsi={snapshot.rsi_14:.1f} trend={snapshot.trend_15m}",
        )

    return Decision(
        inst_id=snapshot.inst_id,
        action="WAIT",
        confidence=40.0,
        reason=f"no paper signal rsi={snapshot.rsi_14:.1f}",
    )


def _seed_paper_tickers(
    exchange: PaperAdapter,
    snapshots: dict[str, MarketSnapshot],
) -> None:
    for inst_id, snap in snapshots.items():
        last = snap.price or DEFAULT_SEED_PRICES.get(inst_id, 100.0)
        spread = max(last * 0.0001, 1e-8)
        exchange.set_ticker(
            Ticker(
                inst_id=inst_id,
                last=last,
                bid=last - spread,
                ask=last + spread,
                open_24h=last,
                high_24h=last * 1.01,
                low_24h=last * 0.99,
                vol_24h=1_000_000.0,
                timestamp=snap.timestamp,
            )
        )


def run_paper_cycle(
    *,
    exchange: PaperAdapter | None = None,
    ledger: KeelLedger | None = None,
    instrument_ids: list[str] | None = None,
    seed_prices: dict[str, float] | None = None,
    force_action: str | None = None,
) -> dict[str, Any]:
    """
    Run one vertical paper/demo cycle through Keel.

    Returns a JSON-serializable summary for tests and CLI.
    """
    settings = get_settings()
    pool = InstrumentPool()
    ids = instrument_ids or [i.inst_id for i in pool.all()]
    prices = {**DEFAULT_SEED_PRICES, **(seed_prices or {})}

    exchange = exchange or PaperAdapter(initial_balance=10_000.0)
    if ledger is None:
        db_path = settings.data_dir / "keel_ledger.db"
        ledger = KeelLedger(db_path)

    now = time.time()
    snapshots: dict[str, MarketSnapshot] = {}
    for inst_id in ids:
        inst = pool.get(inst_id)
        name = inst.name if inst else inst_id.split("-")[0]
        base = prices.get(inst_id, 100.0)
        candles = build_synthetic_candles(base, now=now)
        snap = MarketSnapshot(
            inst_id=inst_id,
            name=name,
            timestamp=now,
            bid=candles[-1].close * 0.9999,
            ask=candles[-1].close * 1.0001,
            candles_15m=candles,
            candles_1h=candles[::4] or candles,
            candles_4h=candles[::16] or candles,
        )
        snapshots[inst_id] = enrich_snapshot(snap)

    _seed_paper_tickers(exchange, snapshots)

    orchestrator = ExecutionOrchestrator(exchange=exchange, ledger=ledger)
    daily_pnl = ledger.get_daily_pnl()

    results: list[dict[str, Any]] = []
    decisions: dict[str, Decision] = {}

    for inst_id, snap in snapshots.items():
        decision = rule_based_decision(snap)
        if force_action and inst_id == ids[0]:
            # Test hook: force a fillable long on first instrument.
            price = snap.price
            atr = max(snap.atr_14, price * 0.01)
            if force_action.upper() == "BUY_LONG":
                decision = Decision(
                    inst_id=inst_id,
                    action="BUY_LONG",
                    confidence=80.0,
                    entry_price=price * 1.001,  # >= ask → paper fill
                    take_profit=price + 2.2 * atr,
                    stop_loss=price - 1.0 * atr,
                    leverage=3,
                    margin_usdt=50.0,
                    reason="forced paper long",
                )
            elif force_action.upper() == "WAIT":
                decision = Decision(inst_id=inst_id, action="WAIT", reason="forced wait")

        decisions[inst_id] = decision
        ledger.record_decision(
            DecisionRecord(
                timestamp=now,
                inst_id=inst_id,
                action=decision.action,
                confidence=decision.confidence,
                entry_price=decision.entry_price,
                take_profit=decision.take_profit,
                stop_loss=decision.stop_loss,
                reason=decision.reason,
            )
        )

        exec_result: ExecutionResult = orchestrator.execute_decision(
            decision,
            daily_pnl=daily_pnl,
        )
        results.append(
            {
                "inst_id": inst_id,
                "action": decision.action,
                "success": exec_result.success,
                "order_id": exec_result.order_id,
                "error": exec_result.error,
                "risk_gate_failed": exec_result.risk_gate_failed,
                "price": exec_result.price,
                "size": exec_result.size,
                "rsi": round(snap.rsi_14, 2),
                "trend": snap.trend_15m,
            }
        )
        if exec_result.risk_gate_failed:
            ledger.record_event(
                "risk_gate_blocked",
                inst_id=inst_id,
                data={"gate": exec_result.risk_gate_failed, "error": exec_result.error},
            )

    ledger.record_event(
        "paper_cycle_complete",
        data={"instruments": len(ids), "results": len(results)},
    )

    return {
        "ok": True,
        "mode": "paper",
        "branding": "Keel Trader",
        "instruments": len(ids),
        "daily_pnl": daily_pnl,
        "positions": len(exchange.get_positions()),
        "results": results,
        "ledger_db": str(getattr(ledger, "db_path", "")),
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Keel Trader paper/demo vertical cycle")
    parser.add_argument(
        "--force-action",
        default=os.environ.get("KEEL_FORCE_ACTION", ""),
        help="Optional test hook: BUY_LONG or WAIT on first instrument",
    )
    parser.add_argument(
        "--db",
        default="",
        help="Optional SQLite ledger path (default: data/keel_ledger.db)",
    )
    args = parser.parse_args(argv)

    ledger = KeelLedger(Path(args.db)) if args.db else None
    force = args.force_action.strip() or None
    summary = run_paper_cycle(ledger=ledger, force_action=force)
    actions = [r["action"] for r in summary["results"]]
    print(
        f"[Keel Trader] paper cycle ok instruments={summary['instruments']} "
        f"positions={summary['positions']} actions={actions}"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
