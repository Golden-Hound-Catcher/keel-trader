"""
Paper/demo vertical trading cycle for Keel Trader.

Pipeline: factors → decision → risk → execution → ledger.

Default path uses PaperExchange when OKX keys are absent (no shell CLI).
When KEEL_OKX_* (or OKX_* aliases) are set, uses OkxRestAdapter (signed V5).

Product entry: ``python -m keel.worker`` / ``python -m keel.worker.cycle``.

Stage 4: persists factor_snapshots + coherent Decision↔risk↔ledger events so
keel.api can read the latest cycle from SQLite without hitting live OKX.

Stage 5: optional OkxRestAdapter via keel.exchange.factory.build_exchange.
Live/demo OKX path fetches public 15m/1H/4H candles (synthetic fallback on failure).

Stage 6: DecisionPolicy port (Stub/Rule/LLM) + modular prompts; default Rule for offline.

Optional notify port (keel.notify): after cycle, POST summary when KEEL_NOTIFY_WEBHOOK_URL set;
respects KEEL_NOTIFY_ALERTS_ONLY / KEEL_NOTIFY_FORMAT (keel|discord).

Monitor: writes worker_cycle_summary to the ledger for GET /api/v1/status last_cycle.
"""
from __future__ import annotations

import argparse
import logging
import math
import os
import sys
import time
from pathlib import Path
from typing import Any

from keel.config import Settings, get_settings
from keel.domain.instruments import InstrumentPool
from keel.exchange.factory import build_exchange, describe_exchange
from keel.exchange.okx_public import fetch_candles
from keel.exchange.okx_rest import OkxRestAdapter
from keel.exchange.paper import PaperAdapter, PaperExchange
from keel.exchange.protocol import ExchangeProtocol, Ticker
from keel.execution.orchestrator import ExecutionOrchestrator, ExecutionResult
from keel.execution.near_probe import (
    evaluate_near_probe,
    record_near_probe_skip,
    should_attempt_near_probe,
)
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



logger = logging.getLogger("keel.worker.cycle")


def _okx_rows_to_candles(rows: list[list[float]]) -> list[Candle]:
    """Convert fetch_candles rows [ts_ms, o, h, l, c, vol] → Candle (oldest→newest)."""
    out: list[Candle] = []
    for row in rows:
        if len(row) < 6:
            continue
        out.append(
            Candle(
                timestamp=float(row[0]) / 1000.0,
                open=float(row[1]),
                high=float(row[2]),
                low=float(row[3]),
                close=float(row[4]),
                volume=float(row[5]),
            )
        )
    return out


def _use_okx_public_candles(
    exchange: ExchangeProtocol,
    settings: Settings,
    *,
    force_paper: bool,
) -> bool:
    """True when cycle should prefer OKX public candles over synthetic."""
    if isinstance(exchange, PaperAdapter):
        return False
    if isinstance(exchange, OkxRestAdapter):
        return True
    return bool(settings.okx_configured and not force_paper)


def _fetch_okx_snapshot_candles(
    inst_id: str,
    *,
    now: float,
    base_price: float,
) -> tuple[list[Candle], list[Candle], list[Candle], str]:
    """
    Fetch distinct 15m / 1H / 4H public candles for multi-TF trends (R5).

    Returns (c15, c1h, c4h, quality_tag). quality_tag is "okx_public" on success
    or "synthetic_fallback:<reason>" when falling back. 1H/4H fall back to
    subsampled finer bars when the coarser fetch fails (still distinct series).
    """
    try:
        rows_15m = fetch_candles(inst_id, bar="15m", limit=64)
        candles_15m = _okx_rows_to_candles(rows_15m)
        if len(candles_15m) < 20:
            raise ValueError(f"insufficient 15m candles ({len(candles_15m)})")
    except Exception as exc:  # noqa: BLE001 — cycle must complete offline-capable
        logger.warning(
            "okx public candles failed inst=%s err=%s; falling back to synthetic",
            inst_id,
            exc,
        )
        candles = build_synthetic_candles(base_price, now=now)
        return candles, candles[::4] or candles, candles[::16] or candles, f"synthetic_fallback:{exc}"

    candles_1h: list[Candle]
    try:
        rows_1h = fetch_candles(inst_id, bar="1H", limit=64)
        candles_1h = _okx_rows_to_candles(rows_1h)
        if len(candles_1h) < 5:
            candles_1h = candles_15m[::4] or candles_15m
    except Exception as exc:  # noqa: BLE001
        logger.warning("okx 1h candles failed inst=%s err=%s; subsample 15m", inst_id, exc)
        candles_1h = candles_15m[::4] or candles_15m

    candles_4h: list[Candle]
    try:
        rows_4h = fetch_candles(inst_id, bar="4H", limit=64)
        candles_4h = _okx_rows_to_candles(rows_4h)
        if len(candles_4h) < 5:
            candles_4h = candles_1h[::4] or candles_1h
    except Exception as exc:  # noqa: BLE001
        logger.warning("okx 4h candles failed inst=%s err=%s; subsample 1h", inst_id, exc)
        candles_4h = candles_1h[::4] or candles_1h

    return candles_15m, candles_1h, candles_4h, "okx_public"



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



def compute_volume_ratio(
    volumes: list[float], *, lookback: int = 20
) -> tuple[float, float]:
    """
    Relative volume vs a trailing window (Rule v3 / enrich semantics).

    ``volume_ratio`` = last_bar_volume / mean(last ``lookback`` bars).
    A value of 1.0 means the latest bar matches the recent average — not a
    mis-scaled percent. Crypto 15m bars are right-skewed, so most bars sit
    below 1.0 (live okx_public: p50≈0.36–0.40, p90≈0.86); requiring ≥1.0
    therefore blocks the large majority of cycles even when other gates align.

    Also returns ``volume_percentile`` ∈ [0, 100]: empirical rank of the last
    bar within the same window (fraction of bars with volume ≤ last × 100).
    """
    if not volumes:
        return 1.0, 50.0
    lb = max(1, int(lookback))
    window = volumes[-lb:] if len(volumes) >= lb else list(volumes)
    avg_vol = sum(window) / float(len(window))
    last = float(volumes[-1])
    ratio = (last / avg_vol) if avg_vol else 1.0
    pct = 100.0 * sum(1 for v in window if float(v) <= last) / float(len(window))
    return float(ratio), float(pct)



def classify_trend_from_candles(candles: list[Candle]) -> str:
    """
    Classify EMA-stack trend on a candle series (oldest → newest).

    Uses the same EMA9/21/55 + price alignment as ``classify_trend``.
    Returns ``neutral`` when the series is too short for a meaningful stack.
    """
    if not candles or len(candles) < 3:
        return "neutral"
    closes = [c.close for c in candles]
    price = float(closes[-1])
    ema_9 = calculate_ema(closes, 9)
    ema_21 = calculate_ema(closes, 21)
    ema_55 = calculate_ema(closes, 55)
    return classify_trend(ema_9, ema_21, ema_55, price)


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
    vol_ratio, vol_pct = compute_volume_ratio(volumes, lookback=20)
    # R5: distinct multi-TF trends — 15m from primary EMA stack; 1h/4h from
    # their own candle series (OKX public 1H/4H or synthetic subsamples).
    trend_15m = classify_trend(ema_9, ema_21, ema_55, price)
    if snapshot.candles_1h:
        trend_1h = classify_trend_from_candles(snapshot.candles_1h)
    else:
        trend_1h = trend_15m
    if snapshot.candles_4h:
        trend_4h = classify_trend_from_candles(snapshot.candles_4h)
    else:
        trend_4h = trend_1h

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
    snapshot.volume_percentile = vol_pct
    snapshot.trend_15m = trend_15m  # type: ignore[assignment]
    snapshot.trend_1h = trend_1h  # type: ignore[assignment]
    snapshot.trend_4h = trend_4h  # type: ignore[assignment]
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



def market_source_from_quality_tag(tag: str) -> str:
    """
    Map one instrument candle quality tag → decision calculus market_source.

    Returns okx_public | synthetic (fallback/unknown → synthetic).
    """
    t = str(tag or "").strip()
    if t == "okx_public":
        return "okx_public"
    # synthetic, synthetic_fallback:*, unknown → synthetic for decision stamp
    return "synthetic"


def market_source_from_quality_tags(tags: list[str]) -> str:
    """
    Aggregate per-instrument candle quality tags for last_cycle.market_source.

    Returns one of: okx_public | synthetic | mixed | unknown.
    """
    kinds: set[str] = set()
    for raw in tags:
        t = str(raw or "").strip()
        if t == "okx_public":
            kinds.add("okx_public")
        elif t == "synthetic" or t.startswith("synthetic_fallback"):
            kinds.add("synthetic")
        else:
            kinds.add("unknown")
    if not kinds:
        return "unknown"
    if kinds == {"okx_public"}:
        return "okx_public"
    if kinds == {"synthetic"}:
        return "synthetic"
    if "okx_public" in kinds and "synthetic" in kinds:
        return "mixed"
    if len(kinds) == 1:
        return next(iter(kinds))
    return "mixed"


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
    market_source: str | None = None,
    quality_tags: list[str] | None = None,
) -> dict[str, Any]:
    """
    Structured last-cycle payload for ledger + GET /api/v1/status.

    Aggregates decision counts by action, risk denies (count + capped reasons),
    and non-risk errors (full ``error_count`` + capped ``errors`` detail list).
    Includes wall-clock ``duration_ms`` for the cycle run.
    ``market_source`` (okx_public|synthetic|mixed|unknown) reflects candle quality;
    when omitted, derived from ``quality_tags`` if provided.
    """
    decision_counts: dict[str, int] = {}
    risk_denies = 0
    risk_deny_reasons: list[dict[str, str]] = []
    error_count = 0
    errors: list[dict[str, Any]] = []
    probe_skip_by_reason: dict[str, int] = {}
    last_probe_skip_reason: str | None = None
    for row in results:
        action = str(row.get("action") or "UNKNOWN")
        decision_counts[action] = decision_counts.get(action, 0) + 1
        skip_r = row.get("near_probe_skip_reason")
        if skip_r:
            key = str(skip_r)
            probe_skip_by_reason[key] = probe_skip_by_reason.get(key, 0) + 1
            last_probe_skip_reason = key
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
    if market_source is None and quality_tags is not None:
        market_source = market_source_from_quality_tags(quality_tags)
    top_skip = None
    if probe_skip_by_reason:
        top_skip = max(probe_skip_by_reason.items(), key=lambda kv: kv[1])[0]
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
        # Q3.5: last-cycle near-probe skip annotation (always when evaluated).
        "probe_skips": int(sum(probe_skip_by_reason.values())),
        "by_skip_reason": probe_skip_by_reason,
        "top_skip_reason": top_skip,
        "last_probe_skip_reason": last_probe_skip_reason,
    }
    if policy_success is not None:
        payload["policy_success"] = policy_success
    if market_source is not None:
        payload["market_source"] = market_source
    # R6: compact multi-TF trends per instrument for status/monitor (soft-fail).
    instrument_trends: list[dict[str, Any]] = []
    for row in results:
        iid = row.get("inst_id")
        if not iid:
            continue
        instrument_trends.append(
            {
                "inst_id": str(iid),
                "trend_15m": row.get("trend_15m", row.get("trend")),
                "trend_1h": row.get("trend_1h"),
                "trend_4h": row.get("trend_4h"),
            }
        )
    payload["instrument_trends"] = instrument_trends
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
    pool = InstrumentPool.from_ids(list(settings.instruments))
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
    use_okx_candles = _use_okx_public_candles(exchange, settings, force_paper=force_paper)
    snapshots: dict[str, MarketSnapshot] = {}
    quality_tags: list[str] = []
    quality_by_inst: dict[str, str] = {}
    for inst_id in ids:
        inst = pool.get(inst_id)
        name = inst.name if inst else inst_id.split("-")[0]
        base = prices.get(inst_id, 100.0)
        quality_tag = "synthetic"
        if use_okx_candles:
            candles_15m, candles_1h, candles_4h, quality_tag = _fetch_okx_snapshot_candles(
                inst_id, now=now, base_price=base
            )
        else:
            candles_15m = build_synthetic_candles(base, now=now)
            candles_1h = candles_15m[::4] or candles_15m
            candles_4h = candles_15m[::16] or candles_15m
        last_close = candles_15m[-1].close if candles_15m else base
        snap = MarketSnapshot(
            inst_id=inst_id,
            name=name,
            timestamp=now,
            bid=last_close * 0.9999,
            ask=last_close * 1.0001,
            candles_15m=candles_15m,
            candles_1h=candles_1h,
            candles_4h=candles_4h,
        )
        enrich_snapshot(snap)
        if quality_tag.startswith("synthetic_fallback"):
            snap.data_quality_reason = quality_tag
        elif quality_tag == "synthetic" and snap.data_valid:
            snap.data_quality_reason = "synthetic"
        elif quality_tag == "okx_public" and snap.data_valid:
            snap.data_quality_reason = "okx_public"
        quality_tags.append(quality_tag)
        quality_by_inst[inst_id] = quality_tag
        snapshots[inst_id] = snap

    # Seed paper tickers only for PaperExchange; OKX REST serves tickers via API.
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
    audit_policy = policy_result.policy_name or policy_label
    modules_used = policy_result.prompt_meta.get("modules_used")
    audit_modules: list[str] | None = None
    if isinstance(modules_used, list):
        audit_modules = [str(m) for m in modules_used]

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
                    "trend_1h": snap.trend_1h,
                    "trend_4h": snap.trend_4h,
                    "data_valid": snap.data_valid,
                    "data_quality_reason": snap.data_quality_reason,
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
                policy_name=audit_policy,
                prompt_modules=audit_modules,
                calculus_data={
                    "leverage": decision.leverage,
                    "margin_usdt": decision.margin_usdt,
                    "valid": decision.valid,
                    "validation_error": decision.validation_error or None,
                    "rsi_14": snap.rsi_14,
                    "trend_15m": snap.trend_15m,
                    "trend_1h": snap.trend_1h,
                    "trend_4h": snap.trend_4h,
                    "policy_name": audit_policy,
                    "prompt_modules": audit_modules,
                    "market_source": market_source_from_quality_tag(
                        quality_by_inst.get(inst_id, "synthetic")
                    ),
                    **(
                        {"signal_diag": decision.signal_diag}
                        if getattr(decision, "signal_diag", None)
                        else {}
                    ),
                },
            )
        )

        # Q3: optional near-signal → shadow_fill probe (kill+shadow+probe only).
        # Policy decision stays WAIT in the ledger; execution may rehearse shadow.
        # Q3.5: when probe evaluates but does not fire, record skip reason.
        exec_decision = decision
        probe_outcome = evaluate_near_probe(
            decision,
            snap,
            kill_switch=settings.kill_switch,
            shadow_mode=settings.shadow_mode,
            probe_enabled=settings.shadow_near_probe,
            max_missing=settings.shadow_near_probe_max_missing,
            min_confidence=settings.shadow_near_probe_min_confidence,
            cooldown_seconds=float(settings.shadow_near_probe_cooldown_seconds),
            ledger=ledger,
            now=now,
            min_edge_bps=settings.shadow_near_probe_min_edge_bps,
            edge_mode=settings.shadow_near_probe_edge_mode,
            fee_role=settings.shadow_fee_role,
            settings=settings,
        )
        probed = probe_outcome.decision
        if probed is not None:
            exec_decision = probed
        elif (
            probe_outcome.skip_reason
            and probe_outcome.skip_reason != "probe_disabled"
            and decision.action == "WAIT"
            and should_attempt_near_probe(
                kill_switch=settings.kill_switch,
                shadow_mode=settings.shadow_mode,
                probe_enabled=settings.shadow_near_probe,
            )
        ):
            # Durable skip for hours-filterable /stats/shadow (not when probe off).
            record_near_probe_skip(
                ledger,
                inst_id=inst_id,
                reason=probe_outcome.skip_reason,
                edge_bps=probe_outcome.edge_bps,
                hurdle_bps=probe_outcome.hurdle_bps,
                fee_role=probe_outcome.fee_role,
                edge_mode=probe_outcome.edge_mode,
                timestamp=now,
            )

        exec_result: ExecutionResult = orchestrator.execute_decision(
            exec_decision,
            daily_pnl=daily_pnl,
            kill_switch=settings.kill_switch,
            shadow_mode=settings.shadow_mode,
        )
        result_row: dict[str, Any] = {
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
            "shadow": getattr(exec_result, "shadow", False),
            "rsi": round(snap.rsi_14, 2),
            "trend": snap.trend_15m,
            "trend_15m": snap.trend_15m,
            "trend_1h": snap.trend_1h,
            "trend_4h": snap.trend_4h,
        }
        if probed is not None:
            result_row["shadow_near_probe"] = True
            result_row["exec_action"] = exec_decision.action
        elif probe_outcome.skip_reason:
            result_row["near_probe_skip_reason"] = probe_outcome.skip_reason
            if probe_outcome.edge_bps is not None:
                result_row["near_probe_edge_bps"] = probe_outcome.edge_bps
            if probe_outcome.hurdle_bps is not None:
                result_row["near_probe_hurdle_bps"] = probe_outcome.hurdle_bps
        if getattr(decision, "signal_diag", None):
            result_row["signal_diag"] = decision.signal_diag
        results.append(result_row)

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
        quality_tags=quality_tags,
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

    notify_payload = cycle_notify_payload(summary)
    summary["notify_alert"] = bool(notify_payload.get("alert"))
    summary["notify_severity"] = notify_payload.get("severity")
    if settings.notify_alerts_only and not notify_payload.get("alert"):
        summary["notify_success"] = True
        summary["notify_skipped"] = True
        summary["notify_detail"] = "alerts_only"
        return summary

    notify_result = notifier.notify(
        NotifyEvent(
            event="trader_cycle_complete",
            payload=notify_payload,
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
