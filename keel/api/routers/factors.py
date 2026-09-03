"""Factor calculation endpoints."""
from __future__ import annotations

from fastapi import APIRouter, HTTPException

from keel.factors import (
    calculate_ema,
    calculate_rsi,
    calculate_atr,
    calculate_macd,
    calculate_bollinger,
)
from keel.exchange import OKXRestAdapter, PaperAdapter
from keel.config import get_settings

router = APIRouter()


def _fetch_candles(inst_id: str, bar: str = "15m", limit: int = 50) -> list[list[float]]:
    """Fetch candles from OKX public API."""
    import urllib.request
    import json

    url = f"https://www.okx.com/api/v5/market/candles?instId={inst_id}&bar={bar}&limit={limit}"
    req = urllib.request.Request(url, headers={"User-Agent": "Keel-Trader/0.1"})
    with urllib.request.urlopen(req, timeout=5) as resp:
        data = json.loads(resp.read().decode("utf-8"))
        if data.get("code") != "0":
            raise ValueError(data.get("msg", "API error"))
        return [[float(x) for x in c[:6]] for c in reversed(data.get("data", []))]


@router.get("/factors/{inst_id}")
def get_factors(inst_id: str):
    """Calculate technical factors for an instrument."""
    try:
        candles = _fetch_candles(inst_id, "15m", 50)
        if not candles:
            raise HTTPException(status_code=404, detail="No candle data")

        closes = [c[4] for c in candles]
        highs = [c[2] for c in candles]
        lows = [c[3] for c in candles]
        volumes = [c[5] for c in candles]

        macd = calculate_macd(closes)
        bb = calculate_bollinger(closes)

        return {
            "inst_id": inst_id,
            "price": closes[-1] if closes else 0,
            "ema_9": round(calculate_ema(closes, 9), 4),
            "ema_21": round(calculate_ema(closes, 21), 4),
            "ema_55": round(calculate_ema(closes, 55), 4),
            "rsi_14": round(calculate_rsi(closes, 14), 2),
            "rsi_7": round(calculate_rsi(closes, 7), 2),
            "atr_14": round(calculate_atr(highs, lows, closes, 14), 4),
            "macd": {
                "line": round(macd.macd_line, 4),
                "signal": round(macd.signal_line, 4),
                "histogram": round(macd.histogram, 4),
            },
            "bollinger": {
                "middle": round(bb.middle, 4),
                "upper": round(bb.upper, 4),
                "lower": round(bb.lower, 4),
                "bandwidth": round(bb.bandwidth, 2),
                "percent_b": round(bb.percent_b, 4),
            },
            "candle_count": len(candles),
        }
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=502, detail=str(e))
