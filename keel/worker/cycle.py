"""
Paper/demo vertical trading cycle for Keel Trader.

Pipeline: factors → decision → risk → execution → ledger.

Default path uses PaperExchange when OKX keys are absent (no shell CLI).
When KEEL_OKX_* (or OKX_* aliases) are set, uses OkxRestAdapter (signed V5).

Product entry: ``python -m keel.worker`` / ``python -m keel.worker.cycle``.

Stage 4: persists factor_snapshots + coherent Decision↔risk↔ledger events so
keel.api can read the latest cycle from SQLite without hitting live OKX.

Stage 5: optional OkxRestAdapter via keel.exchange.factory.build_exchange.

Stage 6: DecisionPolicy port (Stub/Rule/LLM) + modular prompts; default Rule for offline.

Optional notify port (keel.notify): after cycle, POST summary when KEEL_NOTIFY_WEBHOOK_URL set.

Monitor: writes worker_cycle_summary to the ledger for GET /api/v1/status last_cycle.
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
from keel.exchange.factory import build_exchange, describe_exchange
from keel.exchange.paper import PaperAdapter, PaperExchange
from keel.exchange.protocol import ExchangeProtocol, Ticker
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
from keel.ledger import DecisionRecord, FactorSnapshot, KeelLedger
from keel.domain.decision import Decision, DecisionAction, validate_decision
from keel.policy import (
    DecisionPolicy,
    PolicyContext,
    build_decision_policy,
    describe_policy,
    rule_based_decision,
)
from keel.notify import (
    Notifier,
    NotifyEvent,
    build_notifier,
    cycle_notify_payload,
    describe_notifier,
)


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


def decision_from_snapshot(snapshot: MarketSnapshot) -> Decision:
    """Rule-based decision with shared schema / RR validation (compat helper)."""
    return validate_decision(rule_based_decision(snapshot))


def _seed_paper_tickers(
    exchange: PaperAdapter | PaperExchange,
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



# Cap detail lists on cycle summaries (monitor / status payloads).
RISK_DENY_REASONS_CAP = 20
CYCLE_ERRORS_CAP = 20


def build_cycle_summary(
    *,
    timestamp: float,
    mode: str,
    adapter: str,
    policy: str,
    instruments: int,
    results: list[dict[str, Any]],
    policy_success: bool | None = None,
    duration_ms: int = 0,
) -> dict[str, Any]:
    """
    Structured last-cycle payload for ledger + GET /api/v1/status.

    Aggregates decision counts by action, risk denies (count + capped reasons),
    and non-risk errors (full ``error_count`` + capped ``errors`` detail list).
    Includes wall-clock ``duration_ms`` for the cycle run.
    """
    decision_counts: dict[str, int] = {}
    risk_denies = 0
    risk_deny_reasons: list[dict[str, str]] = []
    error_count = 0
    errors: list[dict[str, Any]] = []
    for row in results:
        action = str(row.get("action") or "UNKNOWN")
        decision_counts[action] = decision_counts.get(action, 0) + 1
        if row.get("risk_gate_failed"):
            risk_denies += 1
            if len(risk_deny_reasons) < RISK_DENY_REASONS_CAP:
                gate = str(row["risk_gate_failed"])
                reason = str(row.get("error") or "")
                risk_deny_reasons.append({"gate": gate, "reason": reason})
        elif row.get("error") and not row.get("success"):
            error_count += 1
            if len(errors) < CYCLE_ERRORS_CAP:
                errors.append(
                    {
                        "inst_id": row.get("inst_id"),
                        "error": str(row["error"]),
                    }
                )
    payload: dict[str, Any] = {
        "timestamp": timestamp,
        "mode": mode,
        "adapter": adapter,
        "policy": policy,
        "instruments": instruments,
        "decision_counts": decision_counts,
        "risk_denies": risk_denies,
        "risk_deny_reasons": risk_deny_reasons,
        "error_count": error_count,
        "errors": errors,
        "duration_ms": int(duration_ms),
    }
    if policy_success is not None:
        payload["policy_success"] = policy_success
    return payload


def run_paper_cycle(
    *,
    exchange: ExchangeProtocol | None = None,
    ledger: KeelLedger | None = None,
    instrument_ids: list[str] | None = None,
    seed_prices: dict[str, float] | None = None,
    force_action: DecisionAction | str | None = None,
    force_paper: bool = False,
    policy: DecisionPolicy | None = None,
    notifier: Notifier | None = None,
) -> dict[str, Any]:
    """
    Run one vertical trader cycle through Keel.

    Exchange selection (when ``exchange`` is not injected):
    - OkxRestAdapter if OKX keys are configured (unless force_paper)
    - PaperExchange otherwise

    Decision policy (when ``policy`` is not injected):
    - ``build_decision_policy()`` → Rule by default; LLM when configured + env

    Notifier (when ``notifier`` is not injected):
    - ``build_notifier()`` → Null when ``KEEL_NOTIFY_WEBHOOK_URL`` empty; else Webhook

    Returns a JSON-serializable summary for tests and CLI.
    """
    cycle_t0 = time.perf_counter()
    settings = get_settings()
    pool = InstrumentPool()
    ids = instrument_ids or [i.inst_id for i in pool.all()]
    prices = {**DEFAULT_SEED_PRICES, **(seed_prices or {})}

    exchange = exchange or build_exchange(settings, force_paper=force_paper)
    adapter_label = describe_exchange(exchange)
    if ledger is None:
        ledger = KeelLedger(settings.ledger_path)

    policy = policy or build_decision_policy(settings)
    policy_label = describe_policy(policy)

    notifier = notifier or build_notifier(settings)
    notifier_label = describe_notifier(notifier)

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

    # Paper needs synthetic tickers; OKX REST serves tickers via public API.
    if isinstance(exchange, PaperAdapter):
        _seed_paper_tickers(exchange, snapshots)

    orchestrator = ExecutionOrchestrator(exchange=exchange, ledger=ledger)
    daily_pnl = ledger.get_daily_pnl()

    results: list[dict[str, Any]] = []

    policy_result = policy.decide(
        PolicyContext(
            snapshots=snapshots,
            instrument_ids=ids,
            timestamp=now,
        )
    )
    decisions: dict[str, Decision] = dict(policy_result.decisions)

    for inst_id, snap in snapshots.items():
        ledger.record_factor_snapshot(
            FactorSnapshot(
                timestamp=now,
                inst_id=inst_id,
                price=snap.price,
                rsi_14=snap.rsi_14,
                ema_9=snap.ema_9,
                ema_21=snap.ema_21,
                atr_14=snap.atr_14,
                macd_histogram=snap.macd_histogram,
                trend_15m=str(snap.trend_15m),
                volume_ratio=snap.volume_ratio,
                payload={
                    "rsi_7": snap.rsi_7,
                    "ema_55": snap.ema_55,
                    "atr_pct": snap.atr_pct,
                    "vwap": snap.vwap,
                    "vwap_bias_pct": snap.vwap_bias_pct,
                    "macd_line": snap.macd_line,
                    "macd_signal": snap.macd_signal,
                    "data_valid": snap.data_valid,
                },
            )
        )

        decision = decisions.get(inst_id) or Decision(
            inst_id=inst_id, action="WAIT", reason="policy omitted"
        )
        if force_action and inst_id == ids[0]:
            # Test hook: force a fillable action on first instrument.
            price = snap.price
            atr = max(snap.atr_14, price * 0.01)
            action = force_action.upper()
            if action == "BUY_LONG":
                # Anchor TP/SL to entry so RR stays >= 2 after the fill premium.
                entry = price * 1.001  # >= ask → paper fill
                decision = validate_decision(
                    Decision(
                        inst_id=inst_id,
                        action="BUY_LONG",
                        confidence=80.0,
                        entry_price=entry,
                        take_profit=entry + 2.2 * atr,
                        stop_loss=entry - 1.0 * atr,
                        leverage=3,
                        margin_usdt=50.0,
                        reason="forced paper long",
                    )
                )
            elif action == "SELL_SHORT":
                entry = price * 0.999  # <= bid → paper fill
                decision = validate_decision(
                    Decision(
                        inst_id=inst_id,
                        action="SELL_SHORT",
                        confidence=80.0,
                        entry_price=entry,
                        take_profit=entry - 2.2 * atr,
                        stop_loss=entry + 1.0 * atr,
                        leverage=3,
                        margin_usdt=50.0,
                        reason="forced paper short",
                    )
                )
            elif action == "WAIT":
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
                calculus_data={
                    "leverage": decision.leverage,
                    "margin_usdt": decision.margin_usdt,
                    "valid": decision.valid,
                    "validation_error": decision.validation_error or None,
                    "rsi_14": snap.rsi_14,
                    "trend_15m": snap.trend_15m,
                },
            )
        )

        exec_result: ExecutionResult = orchestrator.execute_decision(
            decision,
            daily_pnl=daily_pnl,
            kill_switch=settings.kill_switch,
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
                "filled": exec_result.filled,
                "resting": exec_result.resting,
                "rsi": round(snap.rsi_14, 2),
                "trend": snap.trend_15m,
            }
        )

    mode = "paper" if isinstance(exchange, PaperAdapter) else "okx_rest"
    duration_ms = max(0, int(round((time.perf_counter() - cycle_t0) * 1000)))
    cycle_summary = build_cycle_summary(
        timestamp=now,
        mode=mode,
        adapter=adapter_label,
        policy=policy_label,
        instruments=len(ids),
        results=results,
        policy_success=policy_result.success,
        duration_ms=duration_ms,
    )
    ledger.record_cycle_summary(cycle_summary)
    ledger.record_event(
        "trader_cycle_complete",
        data={
            "instruments": len(ids),
            "results": len(results),
            "adapter": adapter_label,
            "mode": mode,
            "policy": policy_label,
            "policy_success": policy_result.success,
            "decision_counts": cycle_summary["decision_counts"],
            "risk_denies": cycle_summary["risk_denies"],
            "risk_deny_reasons": cycle_summary["risk_deny_reasons"],
            "error_count": cycle_summary["error_count"],
            "errors": cycle_summary["errors"],
        },
        timestamp=now,
    )
    # Keep legacy event name for older API consumers / tests.
    if mode == "paper":
        ledger.record_event(
            "paper_cycle_complete",
            data={"instruments": len(ids), "results": len(results)},
            timestamp=now,
        )

    summary: dict[str, Any] = {
        "ok": True,
        "mode": mode,
        "adapter": adapter_label,
        "policy": policy_label,
        "policy_success": policy_result.success,
        "branding": "Keel Trader",
        "instruments": len(ids),
        "daily_pnl": daily_pnl,
        "positions": len(exchange.get_positions()),
        "results": results,
        "ledger_db": str(getattr(ledger, "db_path", "")),
        "notifier": notifier_label,
        "cycle_summary": cycle_summary,
    }

    notify_result = notifier.notify(
        NotifyEvent(
            event="trader_cycle_complete",
            payload=cycle_notify_payload(summary),
        )
    )
    summary["notify_success"] = notify_result.success
    summary["notify_skipped"] = notify_result.skipped
    summary["notify_detail"] = notify_result.detail
    return summary


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Keel Trader vertical cycle (paper or OKX REST)")
    parser.add_argument(
        "--force-action",
        default=os.environ.get("KEEL_FORCE_ACTION", ""),
        help="Optional test hook: BUY_LONG / SELL_SHORT / WAIT on first instrument",
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
        f"[Keel Trader] cycle ok mode={summary.get('mode')} "
        f"adapter={summary.get('adapter')} "
        f"instruments={summary['instruments']} "
        f"positions={summary['positions']} actions={actions}"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
