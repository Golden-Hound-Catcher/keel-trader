"""Factor endpoints — prefer SQLite snapshots from the last worker cycle."""
from __future__ import annotations

from fastapi import APIRouter, HTTPException, Query

from keel.api.deps import get_ledger
from keel.api.schemas import BollingerBlock, FactorsResponse, MacdBlock
from keel.exchange.okx_public import fetch_candles
from keel.factors import (
    calculate_atr,
    calculate_bollinger,
    calculate_ema,
    calculate_macd,
    calculate_rsi,
)

router = APIRouter()


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
                volume_ratio=snap.volume_ratio,
            )

    try:
        candles = fetch_candles(inst_id, bar="15m", limit=50)
        if not candles:
            raise HTTPException(status_code=404, detail="No candle data")

        closes = [c[4] for c in candles]
        highs = [c[2] for c in candles]
        lows = [c[3] for c in candles]

        macd = calculate_macd(closes)
        bb = calculate_bollinger(closes)

        return FactorsResponse(
            inst_id=inst_id,
            source="okx_public",
            price=closes[-1] if closes else 0,
            ema_9=round(calculate_ema(closes, 9), 4),
            ema_21=round(calculate_ema(closes, 21), 4),
            ema_55=round(calculate_ema(closes, 55), 4),
            rsi_14=round(calculate_rsi(closes, 14), 2),
            rsi_7=round(calculate_rsi(closes, 7), 2),
            atr_14=round(calculate_atr(highs, lows, closes, 14), 4),
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
        )
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=502, detail=str(e))
