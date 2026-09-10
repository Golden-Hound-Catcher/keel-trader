"""
F5: TradingView-inspired rule variants (public TA concepts — not Pine copy).

- ``supertrend``: ATR-band direction flip entry; HTF filter optional; ATR TP/SL
  geometry unchanged (barrier_exit_markout is the primary offline score).
- ``donchian``: prior-bar Donchian breakout + EMA stack filter + volume≥SMA×k;
  no repaint (channel excludes current bar).

Cool-down is enforced by the walk / live fire_cooldown path, not here.
"""
from __future__ import annotations

import os
from typing import Any

from keel.factors.market_data import MarketSnapshot
from keel.factors.technical import (
    calculate_supertrend,
    donchian_prior_channel,
    volume_sma_ratio,
)

# Defaults (env-overridable).
_ST_ATR_LENGTH_DEFAULT = 10
_ST_FACTOR_DEFAULT = 3.0
_DONCHIAN_PERIOD_DEFAULT = 20
_DONCHIAN_VOL_MULT_DEFAULT = 1.0
_DONCHIAN_VOL_SMA_PERIOD_DEFAULT = 20


def _env_float(key: str, default: float) -> float:
    raw = (os.environ.get(key) or "").strip()
    if not raw:
        return float(default)
    try:
        return float(raw)
    except ValueError:
        return float(default)


def _env_int(key: str, default: int) -> int:
    raw = (os.environ.get(key) or "").strip()
    if not raw:
        return int(default)
    try:
        return int(float(raw))
    except ValueError:
        return int(default)


def _env_bool(key: str, default: bool) -> bool:
    raw = (os.environ.get(key) or "").strip().lower()
    if not raw:
        return bool(default)
    if raw in ("1", "true", "yes", "on"):
        return True
    if raw in ("0", "false", "no", "off"):
        return False
    return bool(default)


def st_atr_length() -> int:
    v = _env_int("KEEL_RULE_ST_ATR_LENGTH", _ST_ATR_LENGTH_DEFAULT)
    return max(2, min(50, v))


def st_factor() -> float:
    v = _env_float("KEEL_RULE_ST_FACTOR", _ST_FACTOR_DEFAULT)
    if v != v:  # NaN
        return float(_ST_FACTOR_DEFAULT)
    return max(0.5, min(10.0, float(v)))


def donchian_period() -> int:
    v = _env_int("KEEL_RULE_DONCHIAN_PERIOD", _DONCHIAN_PERIOD_DEFAULT)
    return max(5, min(100, v))


def donchian_vol_mult() -> float:
    v = _env_float("KEEL_RULE_DONCHIAN_VOL_MULT", _DONCHIAN_VOL_MULT_DEFAULT)
    if v != v:
        return float(_DONCHIAN_VOL_MULT_DEFAULT)
    return max(0.0, min(5.0, float(v)))


def donchian_vol_sma_period() -> int:
    v = _env_int("KEEL_RULE_DONCHIAN_VOL_SMA_PERIOD", _DONCHIAN_VOL_SMA_PERIOD_DEFAULT)
    return max(5, min(100, v))


def _htf_flags(snapshot: MarketSnapshot, *, require_1h: bool, require_4h: bool) -> dict[str, Any]:
    trend_15m = str(snapshot.trend_15m or "neutral")
    trend_1h = str(getattr(snapshot, "trend_1h", "neutral") or "neutral")
    trend_4h = str(getattr(snapshot, "trend_4h", "neutral") or "neutral")
    t15_bull = trend_15m == "bullish"
    t15_bear = trend_15m == "bearish"
    t1h_bull = trend_1h == "bullish"
    t1h_bear = trend_1h == "bearish"
    t4h_bull = trend_4h == "bullish"
    t4h_bear = trend_4h == "bearish"

    if require_1h and require_4h:
        htf_long = t1h_bull and t4h_bull
        htf_short = t1h_bear and t4h_bear
        trend_gate = "1h+4h"
    elif require_1h:
        htf_long = t1h_bull
        htf_short = t1h_bear
        trend_gate = "1h"
    elif require_4h:
        htf_long = t4h_bull
        htf_short = t4h_bear
        trend_gate = "4h"
    else:
        # Soft: align with entry-TF EMA stack when HTF off.
        htf_long = t15_bull or True  # no hard HTF — always ok
        htf_short = t15_bear or True
        # When both off, gates pass unconditionally.
        htf_long = True
        htf_short = True
        trend_gate = "off"

    return {
        "trend_15m": trend_15m,
        "trend_1h": trend_1h,
        "trend_4h": trend_4h,
        "require_1h_trend": bool(require_1h),
        "require_4h_trend": bool(require_4h),
        "trend_gate": trend_gate,
        "htf_long_ok": bool(htf_long),
        "htf_short_ok": bool(htf_short),
        "trend_1h_confirm": (t1h_bull if t15_bull else t1h_bear if t15_bear else False),
        "trend_4h_confirm": (t4h_bull if t15_bull else t4h_bear if t15_bear else False),
    }


def diagnose_supertrend(snapshot: MarketSnapshot) -> dict[str, Any]:
    """Full-gate diagnostics for ``supertrend`` variant."""
    data_ok = bool(snapshot.data_valid) and snapshot.price > 0 and snapshot.atr_14 > 0
    length = st_atr_length()
    factor = st_factor()
    # Align with TF: default hard 1h+4h via same envs (F4-friendly HTF filter).
    require_1h = _env_bool("KEEL_RULE_REQUIRE_1H_TREND", True)
    require_4h = _env_bool("KEEL_RULE_TF_REQUIRE_4H", True)
    htf = _htf_flags(snapshot, require_1h=require_1h, require_4h=require_4h)

    candles = list(snapshot.candles_15m or [])
    highs = [c.high for c in candles]
    lows = [c.low for c in candles]
    closes = [c.close for c in candles]
    st = calculate_supertrend(highs, lows, closes, period=length, factor=factor)

    flip_long = bool(st.flipped and st.direction == 1)
    flip_short = bool(st.flipped and st.direction == -1)
    st_long_ok = flip_long
    st_short_ok = flip_short

    gates: dict[str, Any] = {
        "data_valid": data_ok,
        "rule_variant": "supertrend",
        "st_atr_length": length,
        "st_factor": factor,
        "st_direction": int(st.direction),
        "st_value": float(st.value),
        "st_upper": float(st.upper),
        "st_lower": float(st.lower),
        "st_flipped": bool(st.flipped),
        "st_flip_long": flip_long,
        "st_flip_short": flip_short,
        "st_long_ok": st_long_ok,
        "st_short_ok": st_short_ok,
        # Compatibility placeholders so shared audit consumers stay happy.
        "rsi_long_ok": True,
        "rsi_short_ok": True,
        "trend_bullish": st_long_ok and htf["htf_long_ok"],
        "trend_bearish": st_short_ok and htf["htf_short_ok"],
        "macd_long_ok": True,
        "macd_short_ok": True,
        "macd_lag_bps": 0.0,
        "macd_lag_ok": False,
        "ema_long_ok": True,
        "ema_short_ok": True,
        "volume_ok": True,
        "volume_path": "n/a_supertrend",
        "volume_soft_pass": False,
        "volume_threshold": 0.0,
        "volume_ratio": float(getattr(snapshot, "volume_ratio", 1.0) or 1.0),
        "volume_percentile": getattr(snapshot, "volume_percentile", None),
        "rsi_14": float(getattr(snapshot, "rsi_14", 50.0) or 50.0),
        "rsi_path": "n/a_supertrend",
        "rsi_soft_pass": False,
        "ema_9": float(getattr(snapshot, "ema_9", 0.0) or 0.0),
        "ema_21": float(getattr(snapshot, "ema_21", 0.0) or 0.0),
        "macd_histogram": float(getattr(snapshot, "macd_histogram", 0.0) or 0.0),
        "max_extension_atr": 0.0,
        "extension_atr": None,
        "extension_ok": True,
        "extension_headroom_atr": None,
        "tf_pullback_enabled": False,
        "rsi_pullback_long_max": 0.0,
        "rsi_pullback_short_min": 0.0,
        "pullback_ok": True,
        **htf,
    }

    if not data_ok:
        gates["nearest"] = "none"
        gates["missing"] = ["data_valid"]
        gates["near_ready"] = False
        return gates

    long_missing: list[str] = []
    short_missing: list[str] = []
    if not st_long_ok:
        long_missing.append("st_flip_long")
    if not htf["htf_long_ok"]:
        long_missing.append("htf_long_ok")
    if not st_short_ok:
        short_missing.append("st_flip_short")
    if not htf["htf_short_ok"]:
        short_missing.append("htf_short_ok")

    if len(long_missing) <= len(short_missing):
        nearest = "long"
        missing = long_missing
    else:
        nearest = "short"
        missing = short_missing
    if not missing and st_long_ok and htf["htf_long_ok"]:
        nearest = "long"
    elif not missing and st_short_ok and htf["htf_short_ok"]:
        nearest = "short"
    elif st_long_ok and htf["htf_long_ok"]:
        nearest = "long"
        missing = []
    elif st_short_ok and htf["htf_short_ok"]:
        nearest = "short"
        missing = []

    # Recompute nearest cleanly.
    long_ok = st_long_ok and htf["htf_long_ok"]
    short_ok = st_short_ok and htf["htf_short_ok"]
    if long_ok:
        nearest, missing = "long", []
    elif short_ok:
        nearest, missing = "short", []
    elif len(long_missing) <= len(short_missing):
        nearest, missing = "long", long_missing
    else:
        nearest, missing = "short", short_missing

    gates["nearest"] = nearest
    gates["missing"] = missing
    gates["near_ready"] = len(missing) == 1
    gates["st_full_long"] = bool(long_ok)
    gates["st_full_short"] = bool(short_ok)
    return gates


def diagnose_donchian(snapshot: MarketSnapshot) -> dict[str, Any]:
    """Full-gate diagnostics for ``donchian`` variant."""
    data_ok = bool(snapshot.data_valid) and snapshot.price > 0 and snapshot.atr_14 > 0
    period = donchian_period()
    vol_mult = donchian_vol_mult()
    vol_sma_n = donchian_vol_sma_period()
    require_1h = _env_bool("KEEL_RULE_REQUIRE_1H_TREND", True)
    require_4h = _env_bool("KEEL_RULE_TF_REQUIRE_4H", True)
    htf = _htf_flags(snapshot, require_1h=require_1h, require_4h=require_4h)

    candles = list(snapshot.candles_15m or [])
    highs = [c.high for c in candles]
    lows = [c.low for c in candles]
    closes = [c.close for c in candles]
    volumes = [c.volume for c in candles]
    channel = donchian_prior_channel(highs, lows, period=period)
    close = float(closes[-1]) if closes else 0.0
    upper = float(channel.upper) if channel else 0.0
    lower = float(channel.lower) if channel else 0.0
    channel_ok = channel is not None

    break_long = bool(channel_ok and close > upper)
    break_short = bool(channel_ok and close < lower)

    ema_9 = float(getattr(snapshot, "ema_9", 0.0) or 0.0)
    ema_21 = float(getattr(snapshot, "ema_21", 0.0) or 0.0)
    ema_long_ok = ema_9 >= ema_21
    ema_short_ok = ema_9 <= ema_21

    vol_ratio = volume_sma_ratio(volumes, period=vol_sma_n)
    # Also accept enrich volume_ratio when SMA window matches (~20).
    enrich_ratio = float(getattr(snapshot, "volume_ratio", 0.0) or 0.0)
    effective_vol = max(vol_ratio, enrich_ratio)
    volume_ok = bool(effective_vol >= float(vol_mult)) if vol_mult > 0 else True

    dc_long_ok = break_long and ema_long_ok and volume_ok
    dc_short_ok = break_short and ema_short_ok and volume_ok

    gates: dict[str, Any] = {
        "data_valid": data_ok,
        "rule_variant": "donchian",
        "donchian_period": period,
        "donchian_upper": upper,
        "donchian_lower": lower,
        "donchian_channel_ok": channel_ok,
        "donchian_break_long": break_long,
        "donchian_break_short": break_short,
        "donchian_vol_mult": vol_mult,
        "donchian_vol_sma_period": vol_sma_n,
        "donchian_vol_ratio": float(vol_ratio),
        "dc_long_ok": dc_long_ok,
        "dc_short_ok": dc_short_ok,
        "rsi_long_ok": True,
        "rsi_short_ok": True,
        "trend_bullish": break_long and htf["htf_long_ok"],
        "trend_bearish": break_short and htf["htf_short_ok"],
        "macd_long_ok": True,
        "macd_short_ok": True,
        "macd_lag_bps": 0.0,
        "macd_lag_ok": False,
        "ema_long_ok": ema_long_ok,
        "ema_short_ok": ema_short_ok,
        "volume_ok": volume_ok,
        "volume_path": "sma_ratio",
        "volume_soft_pass": False,
        "volume_threshold": float(vol_mult),
        "volume_ratio": float(effective_vol),
        "volume_percentile": getattr(snapshot, "volume_percentile", None),
        "rsi_14": float(getattr(snapshot, "rsi_14", 50.0) or 50.0),
        "rsi_path": "n/a_donchian",
        "rsi_soft_pass": False,
        "ema_9": ema_9,
        "ema_21": ema_21,
        "macd_histogram": float(getattr(snapshot, "macd_histogram", 0.0) or 0.0),
        "max_extension_atr": 0.0,
        "extension_atr": None,
        "extension_ok": True,
        "extension_headroom_atr": None,
        "tf_pullback_enabled": False,
        "rsi_pullback_long_max": 0.0,
        "rsi_pullback_short_min": 0.0,
        "pullback_ok": True,
        **htf,
    }

    if not data_ok:
        gates["nearest"] = "none"
        gates["missing"] = ["data_valid"]
        gates["near_ready"] = False
        return gates

    def _side_missing(side: str) -> list[str]:
        miss: list[str] = []
        if side == "long":
            if not channel_ok:
                miss.append("donchian_channel_ok")
            elif not break_long:
                miss.append("donchian_break_long")
            if not ema_long_ok:
                miss.append("ema_long_ok")
            if not volume_ok:
                miss.append("volume_ok")
            if not htf["htf_long_ok"]:
                miss.append("htf_long_ok")
        else:
            if not channel_ok:
                miss.append("donchian_channel_ok")
            elif not break_short:
                miss.append("donchian_break_short")
            if not ema_short_ok:
                miss.append("ema_short_ok")
            if not volume_ok:
                miss.append("volume_ok")
            if not htf["htf_short_ok"]:
                miss.append("htf_short_ok")
        return miss

    long_missing = _side_missing("long")
    short_missing = _side_missing("short")
    long_ok = len(long_missing) == 0
    short_ok = len(short_missing) == 0
    if long_ok:
        nearest, missing = "long", []
    elif short_ok:
        nearest, missing = "short", []
    elif len(long_missing) <= len(short_missing):
        nearest, missing = "long", long_missing
    else:
        nearest, missing = "short", short_missing

    gates["nearest"] = nearest
    gates["missing"] = missing
    gates["near_ready"] = len(missing) == 1
    gates["dc_full_long"] = bool(long_ok)
    gates["dc_full_short"] = bool(short_ok)
    return gates


def tv_long_short_ok(diag: dict[str, Any]) -> tuple[bool, bool]:
    """Return (long_ok, short_ok) for TV variants from diagnose output."""
    variant = str(diag.get("rule_variant") or "")
    if variant == "supertrend":
        return bool(diag.get("st_full_long")), bool(diag.get("st_full_short"))
    if variant == "donchian":
        return bool(diag.get("dc_full_long")), bool(diag.get("dc_full_short"))
    return False, False


__all__ = [
    "diagnose_donchian",
    "diagnose_supertrend",
    "donchian_period",
    "donchian_vol_mult",
    "donchian_vol_sma_period",
    "st_atr_length",
    "st_factor",
    "tv_long_short_ok",
]
