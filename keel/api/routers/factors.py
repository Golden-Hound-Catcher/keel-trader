"""Factor endpoints — prefer SQLite snapshots from the last worker cycle."""
from __future__ import annotations

from typing import Any

from fastapi import APIRouter, HTTPException, Query

from keel.api.deps import get_ledger
from keel.api.schemas import BollingerBlock, FactorsResponse, MacdBlock
from keel.exchange.okx_public import fetch_candles
from keel.factors import (
    calculate_atr,
    calculate_bollinger,
    calculate_ema,
    calculate_keltner,
    calculate_macd,
    calculate_rsi,
    calculate_supertrend,
    calculate_vwap,
    classify_market_regime,
    classify_trend,
    compute_volume_ratio,
    detect_squeeze,
    detect_squeeze_release,
)

router = APIRouter()


def _opt_float(payload: dict[str, Any], key: str) -> float | None:
    raw = payload.get(key)
    if raw is None or raw == "":
        return None
    try:
        return float(raw)
    except (TypeError, ValueError):
        return None


def _opt_int(payload: dict[str, Any], key: str) -> int | None:
    raw = payload.get(key)
    if raw is None or raw == "":
        return None
    try:
        return int(float(raw))
    except (TypeError, ValueError):
        return None


def _opt_bool(payload: dict[str, Any], key: str) -> bool | None:
    if key not in payload:
        return None
    return bool(payload.get(key))


@router.get("/factors/{inst_id}", response_model=FactorsResponse)
def get_factors(
    inst_id: str,
    live: bool = Query(default=False, description="If true, fetch live OKX candles (network)"),
    max_age: int = Query(default=3600, le=86400),
) -> FactorsResponse:
    """
    Return technical factors for an instrument.

    Default: latest factor_snapshot from the Keel ledger (written by worker cycle).
    Pass live=1 to compute from public OKX candles (no credentials).
    """
    if not live:
        ledger = get_ledger()
        snap = ledger.get_latest_factor_snapshot(inst_id, max_age_seconds=max_age)
        if snap is not None:
            payload = snap.payload or {}
            raw_quality = payload.get("data_quality_reason")
            quality = str(raw_quality) if raw_quality not in (None, "") else None
            regime_raw = payload.get("regime")
            return FactorsResponse(
                inst_id=inst_id,
                source="ledger",
                timestamp=snap.timestamp,
                price=snap.price,
                ema_9=round(snap.ema_9, 4),
                ema_21=round(snap.ema_21, 4),
                ema_55=round(float(payload.get("ema_55", 0) or 0), 4),
                rsi_14=round(snap.rsi_14, 2),
                rsi_7=round(float(payload.get("rsi_7", 0) or 0), 2),
                atr_14=round(snap.atr_14, 4),
                macd=MacdBlock(
                    line=round(float(payload.get("macd_line", 0) or 0), 4),
                    signal=round(float(payload.get("macd_signal", 0) or 0), 4),
                    histogram=round(snap.macd_histogram, 4),
                ),
                trend_15m=snap.trend_15m,
                trend_1h=(
                    str(payload["trend_1h"])
                    if payload.get("trend_1h") not in (None, "")
                    else None
                ),
                trend_4h=(
                    str(payload["trend_4h"])
                    if payload.get("trend_4h") not in (None, "")
                    else None
                ),
                volume_ratio=snap.volume_ratio,
                data_quality_reason=quality,
                volume_percentile=_opt_float(payload, "volume_percentile"),
                vwap=_opt_float(payload, "vwap"),
                vwap_bias_pct=_opt_float(payload, "vwap_bias_pct"),
                supertrend=_opt_float(payload, "supertrend"),
                supertrend_direction=_opt_int(payload, "supertrend_direction"),
                bb_percent_b=_opt_float(payload, "bb_percent_b"),
                squeeze=_opt_bool(payload, "squeeze"),
                squeeze_prev=_opt_bool(payload, "squeeze_prev"),
                squeeze_release=_opt_bool(payload, "squeeze_release"),
                regime=str(regime_raw) if regime_raw not in (None, "") else None,
            )

    try:
        candles = fetch_candles(inst_id, bar="15m", limit=50)
        if not candles:
            raise HTTPException(status_code=404, detail="No candle data")

        closes = [c[4] for c in candles]
        highs = [c[2] for c in candles]
        lows = [c[3] for c in candles]
        volumes = [c[5] if len(c) > 5 else 0.0 for c in candles]

        macd = calculate_macd(closes)
        bb = calculate_bollinger(closes)
        atr = calculate_atr(highs, lows, closes, 14)
        ema_9 = calculate_ema(closes, 9)
        ema_21 = calculate_ema(closes, 21)
        ema_55 = calculate_ema(closes, 55)
        price = closes[-1] if closes else 0
        trend_15m = classify_trend(ema_9, ema_21, ema_55, price)
        vol_ratio, vol_pct = compute_volume_ratio(volumes, lookback=20)
        vwap = calculate_vwap(closes, volumes)
        vwap_bias = ((price - vwap) / vwap * 100.0) if vwap else 0.0
        st = calculate_supertrend(highs, lows, closes)
        kc = calculate_keltner(highs, lows, closes)
        squeeze = detect_squeeze(bb, kc)
        squeeze_prev = False
        if len(closes) >= 21:
            squeeze_prev = detect_squeeze(
                calculate_bollinger(closes[:-1]),
                calculate_keltner(highs[:-1], lows[:-1], closes[:-1]),
            )
        squeeze_release = detect_squeeze_release(
            squeeze_now=bool(squeeze), squeeze_prev=bool(squeeze_prev)
        )
        last_range = float(highs[-1] - lows[-1]) if highs and lows else 0.0
        regime = classify_market_regime(
            squeeze=bool(squeeze),
            supertrend_direction=int(st.direction) if st.valid else 0,
            trend_1h=str(trend_15m),
            bar_range=last_range,
            atr=float(atr),
        )

        return FactorsResponse(
            inst_id=inst_id,
            source="okx_public",
            price=price,
            ema_9=round(ema_9, 4),
            ema_21=round(ema_21, 4),
            ema_55=round(ema_55, 4),
            rsi_14=round(calculate_rsi(closes, 14), 2),
            rsi_7=round(calculate_rsi(closes, 7), 2),
            atr_14=round(atr, 4),
            macd=MacdBlock(
                line=round(macd.macd_line, 4),
                signal=round(macd.signal_line, 4),
                histogram=round(macd.histogram, 4),
            ),
            bollinger=BollingerBlock(
                middle=round(bb.middle, 4),
                upper=round(bb.upper, 4),
                lower=round(bb.lower, 4),
                bandwidth=round(bb.bandwidth, 2),
                percent_b=round(bb.percent_b, 4),
            ),
            candle_count=len(candles),
            trend_15m=str(trend_15m),
            volume_ratio=round(float(vol_ratio), 4),
            data_quality_reason="okx_public",
            volume_percentile=round(float(vol_pct), 2) if vol_pct is not None else None,
            vwap=round(float(vwap), 4),
            vwap_bias_pct=round(float(vwap_bias), 4),
            supertrend=round(float(st.value), 4),
            supertrend_direction=int(st.direction) if st.valid else 0,
            bb_percent_b=round(float(bb.percent_b), 4),
            squeeze=bool(squeeze),
            squeeze_prev=bool(squeeze_prev),
            squeeze_release=bool(squeeze_release),
            regime=str(regime),
        )
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=502, detail=str(e))
