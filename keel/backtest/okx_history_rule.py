"""
F0b / F2c / F3: offline OKX historical candle backtest under E3.1 rule semantics.

Walks closed entry-TF bars (default 15m; F4 multi-TF passes entry bar size),
builds worker-like snapshots (entry/mid/high confirm slots + enrich),
runs ``diagnose_rule_signal`` / ``rule_based_decision`` under forced TF+E2B+E3.1
env (ext=0, pullback=0), applies per-instrument fire cooldown, and fee-aware
markout on future 15m closes (taker RT ~10 bps hurdle).

F2c adds optional ATR barrier exit markout (TP 2.2×ATR / SL 1.0×ATR on
subsequent 15m OHLC path, else timeout 900s) for strategy comparison vs
fixed-horizon 300s markout.

P0 Supertrend trail (``trail_kind=supertrend``, default) and F6 ATR peak-trail
(``trail_kind=atr`` / ``trail_exit_markout``) are measurement-only. Opt also
adds MFE/MAE excursion and early-TP / 1R scale-out markouts.

Network I/O lives in the CLI script; this module accepts in-memory candle rows
so unit tests stay offline.
"""
from __future__ import annotations

import os
from collections import Counter
from contextlib import contextmanager
from dataclasses import dataclass, field
from typing import Any, Iterator, Sequence

from keel.domain.decision import validate_decision
from keel.exchange.okx_fees import (
    REGULAR_USDT_SWAP_MAKER_BPS,
    REGULAR_USDT_SWAP_TAKER_BPS,
)
from keel.exchange.okx_public import bar_duration_seconds
from keel.factors.market_data import Candle, MarketSnapshot
from keel.factors.technical import (
    DEFAULT_SUPERTREND_MULTIPLIER,
    DEFAULT_SUPERTREND_PERIOD,
    calculate_supertrend_series,
)
from keel.ledger.full_gate import is_full_gate_fire
from keel.ledger.near_entry_markout import (
    DEFAULT_CLEAR_HORIZON_SECONDS,
    DEFAULT_CLEAR_HURDLE_BPS,
)
from keel.ledger.shadow_markout import (
    DEFAULT_MARKOUT_HORIZONS_SECONDS,
    markout_bps,
)
from keel.ledger.tf_fire_replay import forced_rule_variant, normalize_variant
from keel.policy.stub import diagnose_rule_signal, rule_based_decision
from keel.worker.cycle import enrich_snapshot, okx_rows_to_candles

# Snapshot lookback mirrors worker public fetch (64 bars).
DEFAULT_LOOKBACK_BARS = 64
DEFAULT_COOLDOWN_SECONDS = 900
BAR_15M_SECONDS = 900

# F2c barrier geometry (mirrors rule / near-probe TP/SL).
DEFAULT_BARRIER_TP_ATR = 2.2
DEFAULT_BARRIER_SL_ATR = 1.0
DEFAULT_BARRIER_TIMEOUT_SECONDS = 900

DEFAULT_HARD_TIME_STOP_SECONDS = 14_400  # 4h = 16×15m (P5 measurement)

# P0 Supertrend trail (measurement) — distinct from F6 ATR peak-trail below.
DEFAULT_TRAIL_TIMEOUT_SECONDS = 14_400  # 4h = 16×15m
DEFAULT_TRAIL_SL_ATR = 1.0

# MFE / early-TP measurement (opt).
DEFAULT_MFE_TIMEOUT_SECONDS = 14_400
DEFAULT_MFE_SNAPSHOT_SECONDS = 900
DEFAULT_MFE_LEVELS_R: tuple[float, ...] = (0.5, 1.0, 1.5, 2.2)
DEFAULT_EARLY_TP_ATR = 1.0
DEFAULT_SCALE_FRACTION = 0.5

# Forced E3.1 / E2B / F2a/F2b-off env keys (in-process only — never writes .env).
_ENV_TF_REQUIRE_4H = "KEEL_RULE_TF_REQUIRE_4H"
_ENV_TF_MACD_LAG = "KEEL_RULE_TF_MACD_LAG_BPS"
_ENV_TF_MAX_EXT = "KEEL_RULE_TF_MAX_EXTENSION_ATR"
_ENV_TF_PULLBACK = "KEEL_RULE_TF_PULLBACK"
_ENV_TF_RSI_PB_LONG = "KEEL_RULE_TF_RSI_PULLBACK_LONG_MAX"
_ENV_TF_RSI_PB_SHORT = "KEEL_RULE_TF_RSI_PULLBACK_SHORT_MIN"


@dataclass
class HistorySeries:
    """Aligned multi-TF candle history for one instrument (oldest→newest).

    Slot names are legacy: ``candles_15m`` = entry TF, ``candles_1h`` = mid
    confirm, ``candles_4h`` = high confirm. ``entry_bar`` records the OKX bar
    size for the entry slot (default ``15m``).
    """

    inst_id: str
    candles_15m: list[Candle]
    candles_1h: list[Candle]
    candles_4h: list[Candle]
    entry_bar: str = "15m"


@dataclass
class BacktestEntry:
    """One full-gate fire recorded during the walk."""

    inst_id: str
    action: str
    entry_ts: float
    entry_price: float
    signal_diag: dict[str, Any]
    markouts: dict[int, dict[str, Any]] = field(default_factory=dict)
    suppressed_cooldown: bool = False
    atr_14: float = 0.0
    barrier: dict[str, Any] | None = None
    trail: dict[str, Any] | None = None
    mfe: dict[str, Any] | None = None
    barrier_1r: dict[str, Any] | None = None
    scale: dict[str, Any] | None = None


def rows_to_series(
    inst_id: str,
    rows_15m: Sequence[Sequence[float]],
    rows_1h: Sequence[Sequence[float]],
    rows_4h: Sequence[Sequence[float]],
) -> HistorySeries:
    """Build a HistorySeries from OKX-style numeric rows."""
    return HistorySeries(
        inst_id=inst_id,
        candles_15m=okx_rows_to_candles([list(r) for r in rows_15m]),
        candles_1h=okx_rows_to_candles([list(r) for r in rows_1h]),
        candles_4h=okx_rows_to_candles([list(r) for r in rows_4h]),
    )


def _closed_higher_tf(
    candles: Sequence[Candle],
    *,
    decision_ts: float,
    bar: str,
) -> list[Candle]:
    """
    Higher-TF bars fully closed by ``decision_ts`` (no look-ahead).

    ``decision_ts`` is the close time of the signal 15m bar.
    """
    dur = float(bar_duration_seconds(bar))
    out: list[Candle] = []
    for c in candles:
        if float(c.timestamp) + dur <= decision_ts + 1e-9:
            out.append(c)
    return out


def build_snapshot_at(
    series: HistorySeries,
    *,
    index_15m: int,
    lookback: int = DEFAULT_LOOKBACK_BARS,
    entry_bar_seconds: float | None = None,
    confirm_mid_bar: str | None = None,
    confirm_high_bar: str | None = None,
) -> MarketSnapshot:
    """
    Build + enrich a MarketSnapshot ending at closed entry bar ``index_15m``.

    Uses only history available at that bar's close (entry window + closed
    mid/high confirm). ``entry_bar_seconds`` defaults from ``series.entry_bar``
    or 15m. Confirm bar labels default to 1H/4H (F4 overrides per entry T).
    """
    c15_all = series.candles_15m
    if index_15m < 0 or index_15m >= len(c15_all):
        raise IndexError(f"index_15m out of range: {index_15m}")
    start = max(0, index_15m + 1 - max(1, int(lookback)))
    c15 = list(c15_all[start : index_15m + 1])
    bar_open = float(c15[-1].timestamp)
    if entry_bar_seconds is not None:
        bar_s = float(entry_bar_seconds)
    else:
        entry_bar = getattr(series, "entry_bar", None) or "15m"
        bar_s = float(bar_duration_seconds(entry_bar))
    decision_ts = bar_open + bar_s
    mid_bar = confirm_mid_bar or "1H"
    high_bar = confirm_high_bar or "4H"
    c1h = _closed_higher_tf(series.candles_1h, decision_ts=decision_ts, bar=mid_bar)
    c4h = _closed_higher_tf(series.candles_4h, decision_ts=decision_ts, bar=high_bar)
    # Keep lookback tail on higher TF (mirror worker ~64).
    lb = max(1, int(lookback))
    if len(c1h) > lb:
        c1h = c1h[-lb:]
    if len(c4h) > lb:
        c4h = c4h[-lb:]
    # If higher TF sparse, subsample from closed 15m only (no look-ahead).
    if len(c1h) < 5:
        c1h = c15[::4] or c15
    if len(c4h) < 5:
        c4h = c15[::16] or c1h[::4] or c1h

    last_close = float(c15[-1].close)
    name = series.inst_id.split("-")[0]
    snap = MarketSnapshot(
        inst_id=series.inst_id,
        name=name,
        timestamp=decision_ts,
        price=last_close,
        bid=last_close * 0.9999,
        ask=last_close * 1.0001,
        candles_15m=c15,
        candles_1h=c1h,
        candles_4h=c4h,
    )
    enrich_snapshot(snap)
    if snap.data_valid:
        snap.data_quality_reason = "okx_history"
    return snap


def price_at_horizon(
    candles_15m: Sequence[Candle],
    *,
    entry_ts: float,
    horizon_seconds: float,
    bar_seconds: float | None = None,
) -> tuple[float, float, str] | None:
    """
    Price at ``entry_ts + horizon`` from entry-TF closes.

    Prefers linear interpolation between adjacent bar closes (mid-path proxy).
    Falls back to first close at/after target. Returns
    ``(price_ts, price, source)`` or None.

    ``bar_seconds`` defaults to 15m (900); F4 multi-TF passes entry bar length.
    """
    if not candles_15m:
        return None
    bar_s = float(BAR_15M_SECONDS if bar_seconds is None else bar_seconds)
    target = float(entry_ts) + max(0.0, float(horizon_seconds))
    # Close times for each bar.
    closes_meta: list[tuple[float, float]] = []
    for c in candles_15m:
        close_ts = float(c.timestamp) + bar_s
        closes_meta.append((close_ts, float(c.close)))
    # Exact / past last
    if target <= closes_meta[0][0]:
        # Before first close — use first close if entry was that bar.
        return closes_meta[0][0], closes_meta[0][1], "first_close"
    for i in range(len(closes_meta) - 1):
        t0, p0 = closes_meta[i]
        t1, p1 = closes_meta[i + 1]
        if t0 <= target <= t1:
            if t1 <= t0:
                return t1, p1, "close"
            w = (target - t0) / (t1 - t0)
            px = p0 + (p1 - p0) * w
            return target, float(px), "interp_close"
    # Beyond last close — only if target == last (no future).
    t_last, p_last = closes_meta[-1]
    if target <= t_last + 1e-9:
        return t_last, p_last, "last_close"
    return None


def fee_aware_markouts(
    *,
    action: str,
    entry_price: float,
    entry_ts: float,
    candles_15m: Sequence[Candle],
    horizons: Sequence[int] = DEFAULT_MARKOUT_HORIZONS_SECONDS,
    open_fee_bps: float = REGULAR_USDT_SWAP_TAKER_BPS,
    rt_fee_bps: float | None = None,
    clear_hurdle_bps: float = DEFAULT_CLEAR_HURDLE_BPS,
    bar_seconds: float | None = None,
) -> dict[int, dict[str, Any]]:
    """Per-horizon gross + net open / net RT markout dicts (funding ignored)."""
    rt = float(rt_fee_bps) if rt_fee_bps is not None else 2.0 * float(open_fee_bps)
    out: dict[int, dict[str, Any]] = {}
    for h in horizons:
        hi = int(h)
        found = price_at_horizon(
            candles_15m,
            entry_ts=entry_ts,
            horizon_seconds=float(hi),
            bar_seconds=bar_seconds,
        )
        if found is None:
            out[hi] = {
                "horizon_seconds": hi,
                "available": False,
                "gross_bps": None,
                "net_open_bps": None,
                "net_rt_bps": None,
                "clears_hurdle": None,
            }
            continue
        _pts, later_px, source = found
        gross = markout_bps(action, entry_price, later_px)
        if gross is None:
            out[hi] = {
                "horizon_seconds": hi,
                "available": False,
                "gross_bps": None,
                "net_open_bps": None,
                "net_rt_bps": None,
                "clears_hurdle": None,
                "price_source": source,
            }
            continue
        net_open = float(gross) - float(open_fee_bps)
        net_rt = float(gross) - float(rt)
        out[hi] = {
            "horizon_seconds": hi,
            "available": True,
            "later_price": later_px,
            "price_source": source,
            "gross_bps": float(gross),
            "net_open_bps": net_open,
            "net_rt_bps": net_rt,
            "clears_hurdle": net_rt >= float(clear_hurdle_bps),
            "open_fee_bps": float(open_fee_bps),
            "rt_fee_bps": float(rt),
            "clear_hurdle_bps": float(clear_hurdle_bps),
        }
    return out



def barrier_exit_markout(
    *,
    action: str,
    entry_price: float,
    entry_ts: float,
    atr_14: float,
    candles_15m: Sequence[Candle],
    tp_atr: float = DEFAULT_BARRIER_TP_ATR,
    sl_atr: float = DEFAULT_BARRIER_SL_ATR,
    timeout_seconds: float = DEFAULT_BARRIER_TIMEOUT_SECONDS,
    open_fee_bps: float = REGULAR_USDT_SWAP_TAKER_BPS,
    rt_fee_bps: float | None = None,
    clear_hurdle_bps: float = DEFAULT_CLEAR_HURDLE_BPS,
    bar_seconds: float | None = None,
) -> dict[str, Any]:
    """
    Fee-aware ATR barrier exit on subsequent entry-TF OHLC path (F2c/F4).

    Long: TP = entry + tp_atr×ATR, SL = entry − sl_atr×ATR.
    Short: mirrored. Walks bars with open ≥ entry_ts; if both TP and SL print
    in the same bar, assume SL first (conservative). Else exit at timeout
    (``timeout_seconds``, default 900) via ``price_at_horizon``.
    ``bar_seconds`` defaults to 15m; F4 passes entry bar length.
    """
    rt = float(rt_fee_bps) if rt_fee_bps is not None else 2.0 * float(open_fee_bps)
    bar_s = float(BAR_15M_SECONDS if bar_seconds is None else bar_seconds)
    act = str(action or "").upper()
    atr = float(atr_14 or 0.0)
    entry = float(entry_price)
    base: dict[str, Any] = {
        "available": False,
        "tp_atr": float(tp_atr),
        "sl_atr": float(sl_atr),
        "timeout_seconds": float(timeout_seconds),
        "atr_14": atr,
        "entry_price": entry,
        "open_fee_bps": float(open_fee_bps),
        "rt_fee_bps": float(rt),
        "clear_hurdle_bps": float(clear_hurdle_bps),
    }
    if atr <= 0.0 or entry <= 0.0:
        return {**base, "reason": "invalid_atr_or_entry"}
    if act == "BUY_LONG":
        tp = entry + float(tp_atr) * atr
        sl = entry - float(sl_atr) * atr
        side = "long"
    elif act == "SELL_SHORT":
        tp = entry - float(tp_atr) * atr
        sl = entry + float(sl_atr) * atr
        side = "short"
    else:
        return {**base, "reason": "unsupported_action"}

    timeout_ts = float(entry_ts) + max(0.0, float(timeout_seconds))
    exit_price: float | None = None
    exit_ts: float | None = None
    exit_reason: str | None = None

    for c in candles_15m:
        bar_open = float(c.timestamp)
        # Only subsequent bars (signal bar already closed at entry_ts).
        if bar_open + 1e-9 < float(entry_ts):
            continue
        if bar_open >= timeout_ts - 1e-9:
            break
        bar_close_ts = bar_open + bar_s
        hi = float(c.high)
        lo = float(c.low)
        if side == "long":
            hit_sl = lo <= sl
            hit_tp = hi >= tp
            if hit_sl and hit_tp:
                exit_price, exit_ts, exit_reason = sl, bar_open, "sl"
            elif hit_sl:
                exit_price, exit_ts, exit_reason = sl, bar_open, "sl"
            elif hit_tp:
                exit_price, exit_ts, exit_reason = tp, bar_open, "tp"
        else:
            hit_sl = hi >= sl
            hit_tp = lo <= tp
            if hit_sl and hit_tp:
                exit_price, exit_ts, exit_reason = sl, bar_open, "sl"
            elif hit_sl:
                exit_price, exit_ts, exit_reason = sl, bar_open, "sl"
            elif hit_tp:
                exit_price, exit_ts, exit_reason = tp, bar_open, "tp"
        if exit_reason is not None:
            break
        if bar_close_ts >= timeout_ts - 1e-9:
            # Timeout falls inside / at end of this bar — use horizon price.
            found = price_at_horizon(
                candles_15m,
                entry_ts=float(entry_ts),
                horizon_seconds=float(timeout_seconds),
                bar_seconds=bar_s,
            )
            if found is None:
                exit_price = float(c.close)
                exit_ts = min(bar_close_ts, timeout_ts)
                exit_reason = "timeout"
            else:
                pts, px, _src = found
                exit_price, exit_ts, exit_reason = float(px), float(pts), "timeout"
            break

    if exit_reason is None:
        # Path exhausted before timeout and no hit — try horizon; else unavailable.
        found = price_at_horizon(
            candles_15m,
            entry_ts=float(entry_ts),
            horizon_seconds=float(timeout_seconds),
            bar_seconds=bar_s,
        )
        if found is None:
            return {**base, "reason": "past_series_end", "tp": tp, "sl": sl}
        pts, px, _src = found
        exit_price, exit_ts, exit_reason = float(px), float(pts), "timeout"

    assert exit_price is not None and exit_ts is not None and exit_reason is not None
    gross = markout_bps(act, entry, float(exit_price))
    if gross is None:
        return {
            **base,
            "reason": "markout_failed",
            "tp": tp,
            "sl": sl,
            "exit_reason": exit_reason,
            "exit_price": float(exit_price),
            "exit_ts": float(exit_ts),
        }
    net_open = float(gross) - float(open_fee_bps)
    net_rt = float(gross) - float(rt)
    hold_s = max(0.0, float(exit_ts) - float(entry_ts))
    return {
        **base,
        "available": True,
        "tp": float(tp),
        "sl": float(sl),
        "exit_reason": exit_reason,
        "exit_price": float(exit_price),
        "exit_ts": float(exit_ts),
        "hold_seconds": hold_s,
        "gross_bps": float(gross),
        "net_open_bps": net_open,
        "net_rt_bps": net_rt,
        "clears_hurdle": net_rt >= float(clear_hurdle_bps),
        "win": net_rt > 0.0,
    }



DEFAULT_TRAIL_ATR = 1.5
DEFAULT_TRAIL_INITIAL_SL_ATR = 1.0


def trail_exit_markout(
    *,
    action: str,
    entry_price: float,
    entry_ts: float,
    atr_14: float,
    candles_15m: Sequence[Candle],
    trail_atr: float = DEFAULT_TRAIL_ATR,
    initial_sl_atr: float = DEFAULT_TRAIL_INITIAL_SL_ATR,
    time_stop_bars: int | None = None,
    timeout_seconds: float = DEFAULT_BARRIER_TIMEOUT_SECONDS,
    open_fee_bps: float = REGULAR_USDT_SWAP_TAKER_BPS,
    rt_fee_bps: float | None = None,
    clear_hurdle_bps: float = DEFAULT_CLEAR_HURDLE_BPS,
    bar_seconds: float | None = None,
) -> dict[str, Any]:
    """
    Fee-aware ATR trailing exit from peak favorable excursion (F6).

    Long: peak = running max high after entry; stop = peak − trail_atr×ATR
    (floored by initial SL = entry − initial_sl_atr×ATR). Short mirrored.
    Optional ``time_stop_bars`` exits at that bar's close if still open.
    Else timeout via ``timeout_seconds`` / ``price_at_horizon``. Same-bar
    conservative: if stop is hit, exit at stop (no peek at favorable close).
    """
    rt = float(rt_fee_bps) if rt_fee_bps is not None else 2.0 * float(open_fee_bps)
    bar_s = float(BAR_15M_SECONDS if bar_seconds is None else bar_seconds)
    act = str(action or "").upper()
    atr = float(atr_14 or 0.0)
    entry = float(entry_price)
    k = float(trail_atr)
    isl = float(initial_sl_atr)
    base: dict[str, Any] = {
        "available": False,
        "trail_atr": k,
        "initial_sl_atr": isl,
        "time_stop_bars": int(time_stop_bars) if time_stop_bars is not None else None,
        "timeout_seconds": float(timeout_seconds),
        "atr_14": atr,
        "entry_price": entry,
        "open_fee_bps": float(open_fee_bps),
        "rt_fee_bps": float(rt),
        "clear_hurdle_bps": float(clear_hurdle_bps),
    }
    if atr <= 0.0 or entry <= 0.0 or k <= 0.0:
        return {**base, "reason": "invalid_atr_or_entry"}
    if act == "BUY_LONG":
        initial_sl = entry - isl * atr
        side = "long"
    elif act == "SELL_SHORT":
        initial_sl = entry + isl * atr
        side = "short"
    else:
        return {**base, "reason": "unsupported_action"}

    timeout_ts = float(entry_ts) + max(0.0, float(timeout_seconds))
    exit_price: float | None = None
    exit_ts: float | None = None
    exit_reason: str | None = None
    peak = entry
    bars_seen = 0
    stop = initial_sl

    for c in candles_15m:
        bar_open = float(c.timestamp)
        if bar_open + 1e-9 < float(entry_ts):
            continue
        if bar_open >= timeout_ts - 1e-9:
            break
        bars_seen += 1
        hi = float(c.high)
        lo = float(c.low)
        bar_close_ts = bar_open + bar_s
        if side == "long":
            peak = max(peak, hi)
            trail_stop = peak - k * atr
            stop = max(initial_sl, trail_stop)
            if lo <= stop:
                exit_price, exit_ts, exit_reason = stop, bar_open, "trail"
        else:
            peak = min(peak, lo)
            trail_stop = peak + k * atr
            stop = min(initial_sl, trail_stop)
            if hi >= stop:
                exit_price, exit_ts, exit_reason = stop, bar_open, "trail"
        if exit_reason is not None:
            break
        if time_stop_bars is not None and bars_seen >= int(time_stop_bars):
            exit_price = float(c.close)
            exit_ts = bar_close_ts
            exit_reason = "time_stop"
            break
        if bar_close_ts >= timeout_ts - 1e-9:
            found = price_at_horizon(
                candles_15m,
                entry_ts=float(entry_ts),
                horizon_seconds=float(timeout_seconds),
                bar_seconds=bar_s,
            )
            if found is None:
                exit_price = float(c.close)
                exit_ts = min(bar_close_ts, timeout_ts)
                exit_reason = "timeout"
            else:
                pts, px, _src = found
                exit_price, exit_ts, exit_reason = float(px), float(pts), "timeout"
            break

    if exit_reason is None:
        found = price_at_horizon(
            candles_15m,
            entry_ts=float(entry_ts),
            horizon_seconds=float(timeout_seconds),
            bar_seconds=bar_s,
        )
        if found is None:
            return {
                **base,
                "reason": "past_series_end",
                "peak": float(peak),
                "stop": float(stop),
            }
        pts, px, _src = found
        exit_price, exit_ts, exit_reason = float(px), float(pts), "timeout"

    assert exit_price is not None and exit_ts is not None and exit_reason is not None
    gross = markout_bps(act, entry, float(exit_price))
    if gross is None:
        return {
            **base,
            "reason": "markout_failed",
            "peak": float(peak),
            "stop": float(stop),
            "exit_reason": exit_reason,
            "exit_price": float(exit_price),
            "exit_ts": float(exit_ts),
        }
    net_open = float(gross) - float(open_fee_bps)
    net_rt = float(gross) - float(rt)
    hold_s = max(0.0, float(exit_ts) - float(entry_ts))
    return {
        **base,
        "available": True,
        "peak": float(peak),
        "stop": float(stop),
        "exit_reason": exit_reason,
        "exit_price": float(exit_price),
        "exit_ts": float(exit_ts),
        "hold_seconds": hold_s,
        "bars_held": int(bars_seen),
        "gross_bps": float(gross),
        "net_open_bps": net_open,
        "net_rt_bps": net_rt,
        "clears_hurdle": net_rt >= float(clear_hurdle_bps),
        "win": net_rt > 0.0,
    }



def excursion_markout(
    *,
    action: str,
    entry_price: float,
    entry_ts: float,
    atr_14: float,
    candles_15m: Sequence[Candle],
    sl_atr: float = DEFAULT_BARRIER_SL_ATR,
    timeout_seconds: float = DEFAULT_MFE_TIMEOUT_SECONDS,
    snapshot_seconds: float = DEFAULT_MFE_SNAPSHOT_SECONDS,
    levels_r: Sequence[float] = DEFAULT_MFE_LEVELS_R,
) -> dict[str, Any]:
    """
    Max favorable / adverse excursion in R (1R = ``sl_atr`` × ATR).

    Walks subsequent 15m OHLC until SL (−1R) or ``timeout_seconds`` (default 4h).
    Same-bar SL+MFE → SL first (conservative). Also snapshots MFE/MAE at
    ``snapshot_seconds`` (default 900s = current barrier hold).
    """
    act = str(action or "").upper()
    atr = float(atr_14 or 0.0)
    entry = float(entry_price)
    levels = tuple(float(x) for x in levels_r)
    base: dict[str, Any] = {
        "available": False,
        "sl_atr": float(sl_atr),
        "timeout_seconds": float(timeout_seconds),
        "snapshot_seconds": float(snapshot_seconds),
        "atr_14": atr,
        "entry_price": entry,
        "levels_r": list(levels),
    }
    if atr <= 0.0 or entry <= 0.0:
        return {**base, "reason": "invalid_atr_or_entry"}
    if act == "BUY_LONG":
        side = "long"
    elif act == "SELL_SHORT":
        side = "short"
    else:
        return {**base, "reason": "unsupported_action"}

    one_r = float(sl_atr) * atr
    sl = entry - one_r if side == "long" else entry + one_r
    timeout_ts = float(entry_ts) + max(0.0, float(timeout_seconds))
    snap_ts = float(entry_ts) + max(0.0, float(snapshot_seconds))
    mfe_r = 0.0
    mae_r = 0.0
    mfe_15m: float | None = None
    mae_15m: float | None = None
    first_touch: dict[str, float | None] = {f"{lv:g}": None for lv in levels}
    first_touch_15m: dict[str, bool] = {f"{lv:g}": False for lv in levels}
    seen_1r = False
    seen_22 = False
    after_1r = "none"
    path_end = "timeout"
    end_ts = timeout_ts

    def _note_fav(fav: float, ts: float) -> None:
        nonlocal mfe_r, seen_1r, seen_22, after_1r
        if fav > mfe_r:
            mfe_r = fav
        for lv in levels:
            key = f"{lv:g}"
            if first_touch[key] is None and mfe_r + 1e-12 >= lv:
                first_touch[key] = ts
        if (not seen_1r) and mfe_r + 1e-12 >= 1.0:
            seen_1r = True
            after_1r = "timeout"
        if (not seen_22) and mfe_r + 1e-12 >= 2.2:
            seen_22 = True
            if seen_1r:
                after_1r = "tp_2_2"

    for c in candles_15m:
        bar_open = float(c.timestamp)
        if bar_open + 1e-9 < float(entry_ts):
            continue
        if bar_open >= timeout_ts - 1e-9:
            break
        bar_close_ts = bar_open + float(BAR_15M_SECONDS)
        hi = float(c.high)
        lo = float(c.low)
        if side == "long":
            hit_sl = lo <= sl
            fav = (hi - entry) / one_r
            adv = (entry - lo) / one_r
        else:
            hit_sl = hi >= sl
            fav = (entry - lo) / one_r
            adv = (hi - entry) / one_r
        if hit_sl:
            # Conservative: do not credit this bar's favorable wick.
            mae_r = max(mae_r, 1.0, adv)
            path_end = "sl"
            end_ts = bar_open
            if seen_1r and after_1r != "tp_2_2":
                after_1r = "sl"
            if mfe_15m is None and bar_close_ts >= snap_ts - 1e-9:
                mfe_15m, mae_15m = mfe_r, mae_r
            break
        _note_fav(fav, bar_open)
        if adv > mae_r:
            mae_r = adv
        if mfe_15m is None and bar_close_ts >= snap_ts - 1e-9:
            mfe_15m, mae_15m = mfe_r, mae_r
            for lv in levels:
                first_touch_15m[f"{lv:g}"] = mfe_r + 1e-12 >= lv
        if bar_close_ts >= timeout_ts - 1e-9:
            path_end = "timeout"
            end_ts = min(bar_close_ts, timeout_ts)
            break
    else:
        path_end = "timeout"
        end_ts = timeout_ts

    if mfe_15m is None:
        mfe_15m, mae_15m = mfe_r, mae_r
        for lv in levels:
            first_touch_15m[f"{lv:g}"] = mfe_r + 1e-12 >= lv

    if not seen_1r:
        after_1r = "none"

    touch = {k: (v is not None) for k, v in first_touch.items()}
    return {
        **base,
        "available": True,
        "one_r_price": float(one_r),
        "mfe_r": float(mfe_r),
        "mae_r": float(mae_r),
        "mfe_r_15m": float(mfe_15m),
        "mae_r_15m": float(mae_15m),
        "touch": touch,
        "touch_15m": dict(first_touch_15m),
        "first_touch_ts": first_touch,
        "path_end": path_end,
        "after_1r": after_1r,
        "end_ts": float(end_ts),
    }


def scale_exit_markout(
    *,
    action: str,
    entry_price: float,
    entry_ts: float,
    atr_14: float,
    candles_15m: Sequence[Candle],
    scale_atr: float = DEFAULT_EARLY_TP_ATR,
    tp_atr: float = DEFAULT_BARRIER_TP_ATR,
    sl_atr: float = DEFAULT_BARRIER_SL_ATR,
    scale_fraction: float = DEFAULT_SCALE_FRACTION,
    timeout_seconds: float = DEFAULT_BARRIER_TIMEOUT_SECONDS,
    open_fee_bps: float = REGULAR_USDT_SWAP_TAKER_BPS,
    rt_fee_bps: float | None = None,
    clear_hurdle_bps: float = DEFAULT_CLEAR_HURDLE_BPS,
) -> dict[str, Any]:
    """
    Fee-aware 1R scale-out then remainder to distant TP (measurement).

    Hit ``scale_atr`` first → close ``scale_fraction`` there; rest to TP/SL/
    timeout. SL before scale → full stop. Timeout before scale → full timeout.
    Same-bar SL+scale → SL first. Combined gross = weighted price moves;
    one round-trip fee on full size (open once, close in one or two clips).
    """
    rt = float(rt_fee_bps) if rt_fee_bps is not None else 2.0 * float(open_fee_bps)
    frac = min(1.0, max(0.0, float(scale_fraction)))
    rest = 1.0 - frac
    first = barrier_exit_markout(
        action=action,
        entry_price=entry_price,
        entry_ts=entry_ts,
        atr_14=atr_14,
        candles_15m=candles_15m,
        tp_atr=float(scale_atr),
        sl_atr=float(sl_atr),
        timeout_seconds=timeout_seconds,
        open_fee_bps=open_fee_bps,
        rt_fee_bps=rt,
        clear_hurdle_bps=clear_hurdle_bps,
    )
    if not first.get("available"):
        return {**first, "scale_fraction": frac, "scaled": False}

    reason1 = str(first.get("exit_reason") or "")
    if reason1 != "tp":
        return {
            **first,
            "scale_fraction": frac,
            "scaled": False,
            "leg1_exit_reason": reason1,
            "combined": True,
        }

    # Scaled at 1R; remainder from same entry, after first exit time.
    second = barrier_exit_markout(
        action=action,
        entry_price=entry_price,
        entry_ts=float(first["exit_ts"]),
        atr_14=atr_14,
        candles_15m=candles_15m,
        tp_atr=float(tp_atr),
        sl_atr=float(sl_atr),
        timeout_seconds=max(
            0.0,
            float(timeout_seconds) - (float(first["exit_ts"]) - float(entry_ts)),
        ),
        open_fee_bps=open_fee_bps,
        rt_fee_bps=rt,
        clear_hurdle_bps=clear_hurdle_bps,
    )
    g1 = float(first.get("gross_bps") or 0.0)
    if not second.get("available"):
        # Remainder marked unavailable — treat rest as timeout at first exit (flat).
        g2 = g1
        reason2 = "unavailable"
        exit_ts = float(first["exit_ts"])
        exit_price = float(first["exit_price"])
    else:
        g2 = float(second.get("gross_bps") or 0.0)
        reason2 = str(second.get("exit_reason") or "unknown")
        exit_ts = float(second.get("exit_ts") or first["exit_ts"])
        exit_price = float(second.get("exit_price") or first["exit_price"])
    gross = frac * g1 + rest * g2
    net_rt = gross - rt
    net_open = gross - float(open_fee_bps)
    tag = {
        "tp": "scale_then_tp",
        "sl": "scale_then_sl",
        "timeout": "scale_then_timeout",
    }.get(reason2, f"scale_then_{reason2}")
    return {
        "available": True,
        "scaled": True,
        "scale_fraction": frac,
        "scale_atr": float(scale_atr),
        "tp_atr": float(tp_atr),
        "sl_atr": float(sl_atr),
        "timeout_seconds": float(timeout_seconds),
        "atr_14": float(atr_14),
        "entry_price": float(entry_price),
        "open_fee_bps": float(open_fee_bps),
        "rt_fee_bps": float(rt),
        "clear_hurdle_bps": float(clear_hurdle_bps),
        "leg1_gross_bps": g1,
        "leg2_gross_bps": g2 if second.get("available") else None,
        "leg1_exit_reason": "tp",
        "leg2_exit_reason": reason2,
        "exit_reason": tag,
        "exit_price": exit_price,
        "exit_ts": exit_ts,
        "hold_seconds": max(0.0, exit_ts - float(entry_ts)),
        "gross_bps": float(gross),
        "net_open_bps": float(net_open),
        "net_rt_bps": float(net_rt),
        "clears_hurdle": net_rt >= float(clear_hurdle_bps),
        "win": net_rt > 0.0,
    }


def _signal_bar_index(candles_15m: Sequence[Candle], entry_ts: float) -> int | None:
    """Index of the closed 15m bar whose close time is ``entry_ts``."""
    best: int | None = None
    for i, c in enumerate(candles_15m):
        close_ts = float(c.timestamp) + float(BAR_15M_SECONDS)
        if close_ts <= float(entry_ts) + 1e-6:
            best = i
    return best


def trailing_supertrend_exit_markout(
    *,
    action: str,
    entry_price: float,
    entry_ts: float,
    atr_14: float,
    candles_15m: Sequence[Candle],
    sl_atr: float = DEFAULT_TRAIL_SL_ATR,
    timeout_seconds: float = DEFAULT_TRAIL_TIMEOUT_SECONDS,
    period: int = DEFAULT_SUPERTREND_PERIOD,
    multiplier: float = DEFAULT_SUPERTREND_MULTIPLIER,
    open_fee_bps: float = REGULAR_USDT_SWAP_TAKER_BPS,
    rt_fee_bps: float | None = None,
    clear_hurdle_bps: float = DEFAULT_CLEAR_HURDLE_BPS,
) -> dict[str, Any]:
    """
    Fee-aware Supertrend trailing exit on subsequent 15m OHLC (P0 measurement).

    No distant TP. Initial SL is the tighter of ``entry ± sl_atr×ATR`` and the
    Supertrend line (protective side). Each *closed* subsequent bar ratchets
    the stop only in the trade's favor (mirrors a 15m worker cycle). Exit on:

    - SL touch during the bar (conservative; uses stop from previous close)
    - Supertrend direction flip vs the position (exit at that bar's close)
    - timeout (default 4h) via ``price_at_horizon``
    """
    rt = float(rt_fee_bps) if rt_fee_bps is not None else 2.0 * float(open_fee_bps)
    act = str(action or "").upper()
    atr = float(atr_14 or 0.0)
    entry = float(entry_price)
    base: dict[str, Any] = {
        "available": False,
        "sl_atr": float(sl_atr),
        "timeout_seconds": float(timeout_seconds),
        "atr_14": atr,
        "entry_price": entry,
        "period": int(period),
        "multiplier": float(multiplier),
        "open_fee_bps": float(open_fee_bps),
        "rt_fee_bps": float(rt),
        "clear_hurdle_bps": float(clear_hurdle_bps),
    }
    if atr <= 0.0 or entry <= 0.0:
        return {**base, "reason": "invalid_atr_or_entry"}
    if act == "BUY_LONG":
        side = "long"
        atr_sl = entry - float(sl_atr) * atr
    elif act == "SELL_SHORT":
        side = "short"
        atr_sl = entry + float(sl_atr) * atr
    else:
        return {**base, "reason": "unsupported_action"}

    highs = [float(c.high) for c in candles_15m]
    lows = [float(c.low) for c in candles_15m]
    closes = [float(c.close) for c in candles_15m]
    st_series = calculate_supertrend_series(
        highs, lows, closes, period=int(period), multiplier=float(multiplier)
    )
    sig_i = _signal_bar_index(candles_15m, entry_ts)
    if sig_i is None or not st_series or sig_i >= len(st_series):
        return {**base, "reason": "missing_signal_bar"}

    st0 = st_series[sig_i]
    if side == "long":
        st_sl = float(st0.value) if st0.direction > 0 else atr_sl
        sl = max(atr_sl, st_sl)
        if sl >= entry:
            sl = atr_sl
    else:
        st_sl = float(st0.value) if st0.direction < 0 else atr_sl
        sl = min(atr_sl, st_sl)
        if sl <= entry:
            sl = atr_sl
    initial_sl = float(sl)

    timeout_ts = float(entry_ts) + max(0.0, float(timeout_seconds))
    exit_price: float | None = None
    exit_ts: float | None = None
    exit_reason: str | None = None
    sl_at_exit = float(sl)

    for k in range(sig_i + 1, len(candles_15m)):
        c = candles_15m[k]
        bar_open = float(c.timestamp)
        if bar_open + 1e-9 < float(entry_ts):
            continue
        if bar_open >= timeout_ts - 1e-9:
            break
        bar_close_ts = bar_open + float(BAR_15M_SECONDS)
        hi = float(c.high)
        lo = float(c.low)
        if side == "long":
            if lo <= sl:
                exit_price, exit_ts, exit_reason = float(sl), bar_open, "sl"
        else:
            if hi >= sl:
                exit_price, exit_ts, exit_reason = float(sl), bar_open, "sl"
        if exit_reason is not None:
            sl_at_exit = float(sl)
            break
        pt = st_series[k] if k < len(st_series) else None
        if pt is not None and pt.direction != 0:
            flipped = (side == "long" and pt.direction < 0) or (
                side == "short" and pt.direction > 0
            )
            if flipped:
                exit_price = float(c.close)
                exit_ts = bar_close_ts
                exit_reason = "flip"
                sl_at_exit = float(sl)
                break
            if side == "long" and pt.direction > 0:
                sl = max(sl, float(pt.value))
            elif side == "short" and pt.direction < 0:
                sl = min(sl, float(pt.value))
        if bar_close_ts >= timeout_ts - 1e-9:
            found = price_at_horizon(
                candles_15m,
                entry_ts=float(entry_ts),
                horizon_seconds=float(timeout_seconds),
            )
            if found is None:
                exit_price = float(c.close)
                exit_ts = min(bar_close_ts, timeout_ts)
            else:
                pts, px, _src = found
                exit_price, exit_ts = float(px), float(pts)
            exit_reason = "timeout"
            sl_at_exit = float(sl)
            break

    if exit_reason is None:
        found = price_at_horizon(
            candles_15m,
            entry_ts=float(entry_ts),
            horizon_seconds=float(timeout_seconds),
        )
        if found is None:
            return {
                **base,
                "reason": "past_series_end",
                "initial_sl": initial_sl,
                "sl": float(sl),
            }
        pts, px, _src = found
        exit_price, exit_ts, exit_reason = float(px), float(pts), "timeout"
        sl_at_exit = float(sl)

    assert exit_price is not None and exit_ts is not None and exit_reason is not None
    gross = markout_bps(act, entry, float(exit_price))
    if gross is None:
        return {
            **base,
            "reason": "markout_failed",
            "initial_sl": initial_sl,
            "sl": sl_at_exit,
            "exit_reason": exit_reason,
            "exit_price": float(exit_price),
            "exit_ts": float(exit_ts),
        }
    net_open = float(gross) - float(open_fee_bps)
    net_rt = float(gross) - float(rt)
    hold_s = max(0.0, float(exit_ts) - float(entry_ts))
    return {
        **base,
        "available": True,
        "initial_sl": initial_sl,
        "sl": sl_at_exit,
        "exit_reason": exit_reason,
        "exit_price": float(exit_price),
        "exit_ts": float(exit_ts),
        "hold_seconds": hold_s,
        "gross_bps": float(gross),
        "net_open_bps": net_open,
        "net_rt_bps": net_rt,
        "clears_hurdle": net_rt >= float(clear_hurdle_bps),
        "win": net_rt > 0.0,
    }


@contextmanager
def forced_e31_rule_env(
    *,
    variant: str = "trend_follow",
    require_4h: bool = True,
    macd_lag_bps: float = 3.0,
    max_extension_atr: float = 0.0,
    pullback: bool = False,
    rsi_pullback_long_max: float | None = None,
    rsi_pullback_short_min: float | None = None,
    st_atr_length: int | None = None,
    st_factor: float | None = None,
    donchian_period: int | None = None,
    donchian_vol_mult: float | None = None,
    require_1h: bool | None = None,
    adx_min: float | None = None,
    adx_period: int | None = None,
    st_entry_mode: str | None = None,
) -> Iterator[str]:
    """Force TF/F5/F6 env + E3.1 require_4h + E2B MACD lag; pin F2a/F2b off by default."""
    keys = {
        _ENV_TF_REQUIRE_4H: "1" if require_4h else "0",
        _ENV_TF_MACD_LAG: str(float(macd_lag_bps)),
        _ENV_TF_MAX_EXT: str(float(max_extension_atr)),
        _ENV_TF_PULLBACK: "1" if pullback else "0",
    }
    if require_1h is not None:
        keys["KEEL_RULE_REQUIRE_1H_TREND"] = "1" if require_1h else "0"
    if st_atr_length is not None:
        keys["KEEL_RULE_ST_ATR_LENGTH"] = str(int(st_atr_length))
    if st_factor is not None:
        keys["KEEL_RULE_ST_FACTOR"] = str(float(st_factor))
    if donchian_period is not None:
        keys["KEEL_RULE_DONCHIAN_PERIOD"] = str(int(donchian_period))
    if donchian_vol_mult is not None:
        keys["KEEL_RULE_DONCHIAN_VOL_MULT"] = str(float(donchian_vol_mult))
    if adx_min is not None:
        keys["KEEL_RULE_ADX_MIN"] = str(float(adx_min))
    if adx_period is not None:
        keys["KEEL_RULE_ADX_PERIOD"] = str(int(adx_period))
    if st_entry_mode is not None:
        keys["KEEL_RULE_ST_ENTRY_MODE"] = str(st_entry_mode).strip().lower() or "flip"
    if pullback and rsi_pullback_long_max is not None:
        keys[_ENV_TF_RSI_PB_LONG] = str(float(rsi_pullback_long_max))
    if pullback and rsi_pullback_short_min is not None:
        keys[_ENV_TF_RSI_PB_SHORT] = str(float(rsi_pullback_short_min))
    prev = {k: os.environ.get(k) for k in keys}
    for k, v in keys.items():
        os.environ[k] = v
    try:
        with forced_rule_variant(variant) as v:
            yield v
    finally:
        for k, old in prev.items():
            if old is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = old


def walk_forward_backtest(
    series_list: Sequence[HistorySeries],
    *,
    variant: str = "trend_follow",
    cooldown_seconds: int = DEFAULT_COOLDOWN_SECONDS,
    lookback: int = DEFAULT_LOOKBACK_BARS,
    horizons: Sequence[int] = DEFAULT_MARKOUT_HORIZONS_SECONDS,
    require_4h: bool = True,
    macd_lag_bps: float = 3.0,
    open_fee_bps: float = REGULAR_USDT_SWAP_TAKER_BPS,
    clear_hurdle_bps: float = DEFAULT_CLEAR_HURDLE_BPS,
    clear_horizon_seconds: int = DEFAULT_CLEAR_HORIZON_SECONDS,
    include_barrier: bool = False,
    barrier_tp_atr: float = DEFAULT_BARRIER_TP_ATR,
    barrier_sl_atr: float = DEFAULT_BARRIER_SL_ATR,
    barrier_timeout_seconds: float = DEFAULT_BARRIER_TIMEOUT_SECONDS,
    max_extension_atr: float = 0.0,
    pullback: bool = False,
    rsi_pullback_long_max: float | None = None,
    rsi_pullback_short_min: float | None = None,
    decision_ts_min: float | None = None,
    decision_ts_max: float | None = None,
    entry_bar: str | None = None,
    confirm_mid_bar: str | None = None,
    confirm_high_bar: str | None = None,
    st_atr_length: int | None = None,
    st_factor: float | None = None,
    donchian_period: int | None = None,
    donchian_vol_mult: float | None = None,
    require_1h: bool | None = None,
    adx_min: float | None = None,
    adx_period: int | None = None,
    st_entry_mode: str | None = None,
    include_trail: bool = False,
    trail_kind: str = "supertrend",
    trail_atr: float = DEFAULT_TRAIL_ATR,
    trail_initial_sl_atr: float = DEFAULT_TRAIL_INITIAL_SL_ATR,
    trail_time_stop_bars: int | None = None,
    trail_sl_atr: float = DEFAULT_TRAIL_SL_ATR,
    trail_timeout_seconds: float = DEFAULT_TRAIL_TIMEOUT_SECONDS,
    trail_period: int = DEFAULT_SUPERTREND_PERIOD,
    trail_multiplier: float = DEFAULT_SUPERTREND_MULTIPLIER,
    include_mfe: bool = False,
    include_early_tp: bool = False,
    mfe_timeout_seconds: float = DEFAULT_MFE_TIMEOUT_SECONDS,
    early_tp_atr: float = DEFAULT_EARLY_TP_ATR,
    scale_fraction: float = DEFAULT_SCALE_FRACTION,
) -> dict[str, Any]:
    """
    Walk closed entry-TF bars per instrument; record full-gate entries + markouts.

    Cooldown: after a full-gate fire, suppress another fire for the same
    ``inst_id`` until ``entry_ts + cooldown_seconds`` (live fire_cooldown
    semantics, in-memory).

    F4: pass ``entry_bar`` (e.g. ``5m``/``1H``/``4H``) so decision close time,
    markout path, and barrier timeout use that bar length; horizons should be
    bar multiples of T (see ``keel.backtest.multitf``).
    """
    variant_n = normalize_variant(variant)
    cd = max(0, int(cooldown_seconds))
    entries: list[BacktestEntry] = []
    n_steps = 0
    n_full_gate = 0
    n_suppressed = 0
    by_inst_action: dict[str, Counter[str]] = {}
    by_inst_steps: Counter[str] = Counter()
    last_fire_ts: dict[str, float] = {}

    with forced_e31_rule_env(
        variant=variant_n,
        require_4h=require_4h,
        macd_lag_bps=macd_lag_bps,
        max_extension_atr=max_extension_atr,
        pullback=pullback,
        rsi_pullback_long_max=rsi_pullback_long_max,
        rsi_pullback_short_min=rsi_pullback_short_min,
        st_atr_length=st_atr_length,
        st_factor=st_factor,
        donchian_period=donchian_period,
        donchian_vol_mult=donchian_vol_mult,
        require_1h=require_1h,
        adx_min=adx_min,
        adx_period=adx_period,
        st_entry_mode=st_entry_mode,
    ):
        for series in series_list:
            inst = series.inst_id
            by_inst_action.setdefault(inst, Counter())
            n = len(series.candles_15m)
            series_entry_bar = (
                entry_bar
                or getattr(series, "entry_bar", None)
                or "15m"
            )
            bar_s = float(bar_duration_seconds(str(series_entry_bar)))
            # Need lookback window; leave room for longest markout.
            max_h = max(int(h) for h in horizons) if horizons else 0
            # Last index we can decide on: need future path for markout optional;
            # still count steps even if markout missing.
            start_i = max(lookback - 1, 20)
            end_i = n - 1
            for i in range(start_i, end_i + 1):
                snap = build_snapshot_at(
                    series,
                    index_15m=i,
                    lookback=lookback,
                    entry_bar_seconds=bar_s,
                    confirm_mid_bar=confirm_mid_bar,
                    confirm_high_bar=confirm_high_bar,
                )
                if not snap.data_valid:
                    continue
                decision_ts = float(snap.timestamp)
                if decision_ts_min is not None and decision_ts < float(decision_ts_min):
                    continue
                if decision_ts_max is not None and decision_ts >= float(decision_ts_max):
                    continue
                n_steps += 1
                by_inst_steps[inst] += 1
                diag = diagnose_rule_signal(snap)
                decision = validate_decision(rule_based_decision(snap))
                action = str(decision.action or "").upper()
                by_inst_action[inst][action] += 1

                if not is_full_gate_fire(
                    action, diag if isinstance(diag, dict) else decision.signal_diag,
                    policy_name="rule",
                ):
                    continue

                entry_ts = float(snap.timestamp)
                # Cooldown suppress
                prev_ts = last_fire_ts.get(inst)
                # Match live full_gate_fire_recent: ts >= now - cooldown counts.
                if cd > 0 and prev_ts is not None and prev_ts >= (entry_ts - float(cd)):
                    n_suppressed += 1
                    entries.append(
                        BacktestEntry(
                            inst_id=inst,
                            action=action,
                            entry_ts=entry_ts,
                            entry_price=float(snap.price),
                            signal_diag=dict(diag or {}),
                            suppressed_cooldown=True,
                        )
                    )
                    continue

                n_full_gate += 1
                last_fire_ts[inst] = entry_ts
                mos = fee_aware_markouts(
                    action=action,
                    entry_price=float(snap.price),
                    entry_ts=entry_ts,
                    candles_15m=series.candles_15m,
                    horizons=horizons,
                    open_fee_bps=open_fee_bps,
                    clear_hurdle_bps=clear_hurdle_bps,
                    bar_seconds=bar_s,
                )
                # Drop markouts that need future beyond series end.
                for h, mo in list(mos.items()):
                    if not mo.get("available"):
                        # Try: if horizon close would be after last bar, mark missing.
                        target = entry_ts + float(h)
                        last_close_ts = (
                            float(series.candles_15m[-1].timestamp) + bar_s
                        )
                        if target > last_close_ts + 1e-9:
                            mos[h] = {
                                **mo,
                                "available": False,
                                "reason": "past_series_end",
                            }
                atr_v = float(getattr(snap, "atr_14", 0.0) or 0.0)
                barrier_row: dict[str, Any] | None = None
                if include_barrier:
                    barrier_row = barrier_exit_markout(
                        action=action,
                        entry_price=float(snap.price),
                        entry_ts=entry_ts,
                        atr_14=atr_v,
                        candles_15m=series.candles_15m,
                        tp_atr=barrier_tp_atr,
                        sl_atr=barrier_sl_atr,
                        timeout_seconds=barrier_timeout_seconds,
                        open_fee_bps=open_fee_bps,
                        clear_hurdle_bps=clear_hurdle_bps,
                        bar_seconds=bar_s,
                    )
                trail_row: dict[str, Any] | None = None
                if include_trail:
                    kind = str(trail_kind or "supertrend").strip().lower()
                    if kind in ("atr", "peak", "f6"):
                        trail_row = trail_exit_markout(
                            action=action,
                            entry_price=float(snap.price),
                            entry_ts=entry_ts,
                            atr_14=atr_v,
                            candles_15m=series.candles_15m,
                            trail_atr=trail_atr,
                            initial_sl_atr=trail_initial_sl_atr,
                            time_stop_bars=trail_time_stop_bars,
                            timeout_seconds=barrier_timeout_seconds,
                            open_fee_bps=open_fee_bps,
                            clear_hurdle_bps=clear_hurdle_bps,
                            bar_seconds=bar_s,
                        )
                    else:
                        trail_row = trailing_supertrend_exit_markout(
                            action=action,
                            entry_price=float(snap.price),
                            entry_ts=entry_ts,
                            atr_14=atr_v,
                            candles_15m=series.candles_15m,
                            sl_atr=trail_sl_atr,
                            timeout_seconds=trail_timeout_seconds,
                            period=trail_period,
                            multiplier=trail_multiplier,
                            open_fee_bps=open_fee_bps,
                            clear_hurdle_bps=clear_hurdle_bps,
                        )
                mfe_row: dict[str, Any] | None = None
                if include_mfe:
                    mfe_row = excursion_markout(
                        action=action,
                        entry_price=float(snap.price),
                        entry_ts=entry_ts,
                        atr_14=atr_v,
                        candles_15m=series.candles_15m,
                        sl_atr=barrier_sl_atr,
                        timeout_seconds=mfe_timeout_seconds,
                    )
                barrier_1r_row: dict[str, Any] | None = None
                scale_row: dict[str, Any] | None = None
                if include_early_tp:
                    barrier_1r_row = barrier_exit_markout(
                        action=action,
                        entry_price=float(snap.price),
                        entry_ts=entry_ts,
                        atr_14=atr_v,
                        candles_15m=series.candles_15m,
                        tp_atr=early_tp_atr,
                        sl_atr=barrier_sl_atr,
                        timeout_seconds=barrier_timeout_seconds,
                        open_fee_bps=open_fee_bps,
                        clear_hurdle_bps=clear_hurdle_bps,
                        bar_seconds=bar_s,
                    )
                    scale_row = scale_exit_markout(
                        action=action,
                        entry_price=float(snap.price),
                        entry_ts=entry_ts,
                        atr_14=atr_v,
                        candles_15m=series.candles_15m,
                        scale_atr=early_tp_atr,
                        tp_atr=barrier_tp_atr,
                        sl_atr=barrier_sl_atr,
                        scale_fraction=scale_fraction,
                        timeout_seconds=barrier_timeout_seconds,
                        open_fee_bps=open_fee_bps,
                        clear_hurdle_bps=clear_hurdle_bps,
                    )
                entries.append(
                    BacktestEntry(
                        inst_id=inst,
                        action=action,
                        entry_ts=entry_ts,
                        entry_price=float(snap.price),
                        signal_diag=dict(diag or {}),
                        markouts=mos,
                        suppressed_cooldown=False,
                        atr_14=atr_v,
                        barrier=barrier_row,
                        trail=trail_row,
                        mfe=mfe_row,
                        barrier_1r=barrier_1r_row,
                        scale=scale_row,
                    )
                )

    fired = [e for e in entries if not e.suppressed_cooldown]
    resolved_entry = entry_bar or (
        series_list[0].entry_bar if series_list else "15m"
    )
    return _summarize(
        n_steps=n_steps,
        n_full_gate=n_full_gate,
        n_suppressed=n_suppressed,
        by_inst_steps=by_inst_steps,
        by_inst_action=by_inst_action,
        fired=fired,
        entries=entries,
        variant=variant_n,
        cooldown_seconds=cd,
        require_4h=require_4h,
        macd_lag_bps=macd_lag_bps,
        open_fee_bps=open_fee_bps,
        clear_hurdle_bps=clear_hurdle_bps,
        clear_horizon_seconds=int(clear_horizon_seconds),
        horizons=list(int(h) for h in horizons),
        include_barrier=include_barrier,
        barrier_timeout_seconds=float(barrier_timeout_seconds),
        include_trail=include_trail,
        trail_kind=str(trail_kind or "supertrend"),
        trail_atr=float(trail_atr),
        trail_time_stop_bars=trail_time_stop_bars,
        trail_sl_atr=float(trail_sl_atr),
        trail_timeout_seconds=float(trail_timeout_seconds),
        trail_period=int(trail_period),
        trail_multiplier=float(trail_multiplier),
        include_mfe=include_mfe,
        include_early_tp=include_early_tp,
        mfe_timeout_seconds=float(mfe_timeout_seconds),
        early_tp_atr=float(early_tp_atr),
        scale_fraction=float(scale_fraction),
        max_extension_atr=float(max_extension_atr),
        pullback=bool(pullback),
        rsi_pullback_long_max=rsi_pullback_long_max,
        rsi_pullback_short_min=rsi_pullback_short_min,
        decision_ts_min=decision_ts_min,
        decision_ts_max=decision_ts_max,
        entry_bar=str(entry_bar or "15m"),
    )


def _avg(vals: list[float]) -> float | None:
    if not vals:
        return None
    return float(sum(vals) / len(vals))


def _win_rate(vals: list[float]) -> float | None:
    if not vals:
        return None
    return sum(1 for v in vals if v > 0) / len(vals)


def reprice_exit_rows(
    rows: Sequence[dict[str, Any]],
    *,
    open_fee_bps: float,
    clear_hurdle_bps: float = DEFAULT_CLEAR_HURDLE_BPS,
    extra: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """
    Reprice already-walked exit rows from ``gross_bps`` (same path, new fees).

    Maker vs taker is a fill-role counterfactual: 100% limit fill assumed,
    queue rejection / timeout-to-taker is not modeled.
    """
    rt = 2.0 * float(open_fee_bps)
    nets: list[float] = []
    reasons: Counter[str] = Counter()
    holds: list[float] = []
    avail = 0
    clears = 0
    for b in rows:
        if not b.get("available"):
            continue
        g = b.get("gross_bps")
        if g is None:
            continue
        avail += 1
        nrt = float(g) - rt
        nets.append(nrt)
        if nrt >= float(clear_hurdle_bps):
            clears += 1
        reasons[str(b.get("exit_reason") or "unknown")] += 1
        hold = b.get("hold_seconds")
        if hold is not None:
            holds.append(float(hold))
    role = "maker"
    if abs(float(open_fee_bps) - float(REGULAR_USDT_SWAP_TAKER_BPS)) < 1e-9:
        role = "taker"
    elif abs(float(open_fee_bps) - float(REGULAR_USDT_SWAP_MAKER_BPS)) < 1e-9:
        role = "maker"
    else:
        role = "custom"
    out: dict[str, Any] = {
        "n_available": avail,
        "avg_net_rt_bps": _avg(nets),
        "win_rate_net_rt": _win_rate(nets),
        "frac_clear_hurdle": (clears / avail) if avail else None,
        "clear_hurdle_bps": float(clear_hurdle_bps),
        "open_fee_bps": float(open_fee_bps),
        "round_trip_fee_bps": rt,
        "fee_role": role,
        "fill_assumption": "limit_fill_100pct_counterfactual",
        "by_exit_reason": dict(reasons),
        "avg_hold_seconds": _avg(holds),
    }
    if extra:
        out.update(extra)
    return out


def _summarize_exit_rows(
    rows: list[dict[str, Any]],
    *,
    clear_hurdle_bps: float,
    extra: dict[str, Any] | None = None,
) -> dict[str, Any]:
    nets: list[float] = []
    reasons: Counter[str] = Counter()
    avail = 0
    clears = 0
    for b in rows:
        if not b.get("available"):
            continue
        avail += 1
        nrt = b.get("net_rt_bps")
        if nrt is not None:
            nets.append(float(nrt))
        if b.get("clears_hurdle"):
            clears += 1
        reasons[str(b.get("exit_reason") or "unknown")] += 1
    out: dict[str, Any] = {
        "n_available": avail,
        "avg_net_rt_bps": _avg(nets),
        "win_rate_net_rt": _win_rate(nets),
        "frac_clear_hurdle": (clears / avail) if avail else None,
        "clear_hurdle_bps": float(clear_hurdle_bps),
        "by_exit_reason": dict(reasons),
    }
    if extra:
        out.update(extra)
    return out


def _summarize_mfe_rows(rows: list[dict[str, Any]]) -> dict[str, Any]:
    avail = [r for r in rows if r.get("available")]
    n = len(avail)
    levels = list(DEFAULT_MFE_LEVELS_R)
    touch: dict[str, int] = {f"{lv:g}": 0 for lv in levels}
    touch_15: dict[str, int] = {f"{lv:g}": 0 for lv in levels}
    after = Counter()
    mfes: list[float] = []
    maes: list[float] = []
    mfes_15: list[float] = []
    maes_15: list[float] = []
    for r in avail:
        mfes.append(float(r.get("mfe_r") or 0.0))
        maes.append(float(r.get("mae_r") or 0.0))
        mfes_15.append(float(r.get("mfe_r_15m") or 0.0))
        maes_15.append(float(r.get("mae_r_15m") or 0.0))
        t = r.get("touch") or {}
        t15 = r.get("touch_15m") or {}
        for lv in levels:
            key = f"{lv:g}"
            if t.get(key):
                touch[key] += 1
            if t15.get(key):
                touch_15[key] += 1
        after[str(r.get("after_1r") or "none")] += 1
    n_1r = n - int(after.get("none") or 0)
    after_among = {
        k: int(v)
        for k, v in after.items()
        if k != "none"
    }
    return {
        "n_available": n,
        "avg_mfe_r": _avg(mfes),
        "avg_mae_r": _avg(maes),
        "avg_mfe_r_15m": _avg(mfes_15),
        "avg_mae_r_15m": _avg(maes_15),
        "touch_frac": {k: (v / n if n else None) for k, v in touch.items()},
        "touch_frac_15m": {k: (v / n if n else None) for k, v in touch_15.items()},
        "touch_count": touch,
        "touch_count_15m": touch_15,
        "after_1r_count": dict(after),
        "n_touched_1r": n_1r,
        "after_1r_frac_among_touched": (
            {k: (v / n_1r) for k, v in after_among.items()} if n_1r else {}
        ),
        "timeout_seconds": DEFAULT_MFE_TIMEOUT_SECONDS,
        "snapshot_seconds": DEFAULT_MFE_SNAPSHOT_SECONDS,
        "one_r": "1.0 ATR SL",
    }


def _summarize(
    *,
    n_steps: int,
    n_full_gate: int,
    n_suppressed: int,
    by_inst_steps: Counter[str],
    by_inst_action: dict[str, Counter[str]],
    fired: list[BacktestEntry],
    entries: list[BacktestEntry],
    variant: str,
    cooldown_seconds: int,
    require_4h: bool,
    macd_lag_bps: float,
    open_fee_bps: float,
    clear_hurdle_bps: float,
    clear_horizon_seconds: int,
    horizons: list[int],
    include_barrier: bool = False,
    barrier_timeout_seconds: float = DEFAULT_BARRIER_TIMEOUT_SECONDS,
    include_trail: bool = False,
    trail_kind: str = "supertrend",
    trail_atr: float = DEFAULT_TRAIL_ATR,
    trail_time_stop_bars: int | None = None,
    trail_sl_atr: float = DEFAULT_TRAIL_SL_ATR,
    trail_timeout_seconds: float = DEFAULT_TRAIL_TIMEOUT_SECONDS,
    trail_period: int = DEFAULT_SUPERTREND_PERIOD,
    trail_multiplier: float = DEFAULT_SUPERTREND_MULTIPLIER,
    include_mfe: bool = False,
    include_early_tp: bool = False,
    mfe_timeout_seconds: float = DEFAULT_MFE_TIMEOUT_SECONDS,
    early_tp_atr: float = DEFAULT_EARLY_TP_ATR,
    scale_fraction: float = DEFAULT_SCALE_FRACTION,
    max_extension_atr: float = 0.0,
    pullback: bool = False,
    rsi_pullback_long_max: float | None = None,
    rsi_pullback_short_min: float | None = None,
    decision_ts_min: float | None = None,
    decision_ts_max: float | None = None,
    entry_bar: str = "15m",
) -> dict[str, Any]:
    by_inst: dict[str, Any] = {}
    for inst, steps in sorted(by_inst_steps.items()):
        fires_i = [e for e in fired if e.inst_id == inst]
        actions = Counter(e.action for e in fires_i)
        by_inst[inst] = {
            "n_steps": int(steps),
            "full_gate": len(fires_i),
            "full_gate_rate": (len(fires_i) / steps) if steps else 0.0,
            "by_action": dict(actions),
            "action_histogram": dict(by_inst_action.get(inst, {})),
        }

    by_action = Counter(e.action for e in fired)
    by_regime = Counter(
        str((e.signal_diag or {}).get("regime") or (e.signal_diag or {}).get("regime_path") or "")
        or "unknown"
        for e in fired
    )
    markout_by_h: dict[str, Any] = {}
    for h in horizons:
        nets: list[float] = []
        grosses: list[float] = []
        clears = 0
        avail = 0
        for e in fired:
            mo = (e.markouts or {}).get(h) or {}
            if not mo.get("available"):
                continue
            avail += 1
            g = mo.get("gross_bps")
            nrt = mo.get("net_rt_bps")
            if g is not None:
                grosses.append(float(g))
            if nrt is not None:
                nets.append(float(nrt))
                if mo.get("clears_hurdle"):
                    clears += 1
        markout_by_h[str(h)] = {
            "horizon_seconds": h,
            "n_available": avail,
            "avg_gross_bps": _avg(grosses),
            "avg_net_rt_bps": _avg(nets),
            "win_rate_net_rt": _win_rate(nets),
            "frac_clear_hurdle": (clears / avail) if avail else None,
            "clear_hurdle_bps": float(clear_hurdle_bps),
        }

    primary = markout_by_h.get(str(clear_horizon_seconds)) or markout_by_h.get("300") or {}

    barrier_summary: dict[str, Any] | None = None
    if include_barrier:
        b_nets: list[float] = []
        b_reasons: Counter[str] = Counter()
        b_avail = 0
        b_clears = 0
        for e in fired:
            b = e.barrier or {}
            if not b.get("available"):
                continue
            b_avail += 1
            nrt = b.get("net_rt_bps")
            if nrt is not None:
                b_nets.append(float(nrt))
            if b.get("clears_hurdle"):
                b_clears += 1
            reason = str(b.get("exit_reason") or "unknown")
            b_reasons[reason] += 1
        barrier_summary = {
            "n_available": b_avail,
            "avg_net_rt_bps": _avg(b_nets),
            "win_rate_net_rt": _win_rate(b_nets),
            "frac_clear_hurdle": (b_clears / b_avail) if b_avail else None,
            "clear_hurdle_bps": float(clear_hurdle_bps),
            "by_exit_reason": dict(b_reasons),
            "tp_atr": DEFAULT_BARRIER_TP_ATR,
            "sl_atr": DEFAULT_BARRIER_SL_ATR,
            "timeout_seconds": float(barrier_timeout_seconds),
            "vs_fixed_horizon_seconds": int(clear_horizon_seconds),
        }

    trail_summary: dict[str, Any] | None = None
    if include_trail:
        t_nets: list[float] = []
        t_reasons: Counter[str] = Counter()
        t_avail = 0
        t_clears = 0
        t_holds: list[float] = []
        for e in fired:
            t = e.trail or {}
            if not t.get("available"):
                continue
            t_avail += 1
            nrt = t.get("net_rt_bps")
            if nrt is not None:
                t_nets.append(float(nrt))
            if t.get("clears_hurdle"):
                t_clears += 1
            reason = str(t.get("exit_reason") or "unknown")
            t_reasons[reason] += 1
            hold = t.get("hold_seconds")
            if hold is not None:
                t_holds.append(float(hold))
        kind = str(trail_kind or "supertrend").strip().lower()
        trail_summary = {
            "n_available": t_avail,
            "avg_net_rt_bps": _avg(t_nets),
            "win_rate_net_rt": _win_rate(t_nets),
            "frac_clear_hurdle": (t_clears / t_avail) if t_avail else None,
            "avg_hold_seconds": _avg(t_holds),
            "clear_hurdle_bps": float(clear_hurdle_bps),
            "by_exit_reason": dict(t_reasons),
            "trail_kind": kind,
            "vs_fixed_horizon_seconds": int(clear_horizon_seconds),
        }
        if kind in ("atr", "peak", "f6"):
            trail_summary["trail_atr"] = float(trail_atr)
            trail_summary["time_stop_bars"] = (
                int(trail_time_stop_bars) if trail_time_stop_bars is not None else None
            )
        else:
            trail_summary["sl_atr"] = float(trail_sl_atr)
            trail_summary["timeout_seconds"] = float(trail_timeout_seconds)
            trail_summary["period"] = int(trail_period)
            trail_summary["multiplier"] = float(trail_multiplier)

    mfe_summary: dict[str, Any] | None = None
    if include_mfe:
        mfe_summary = _summarize_mfe_rows(
            [e.mfe or {} for e in fired]
        )
        mfe_summary["timeout_seconds"] = float(mfe_timeout_seconds)

    barrier_1r_summary: dict[str, Any] | None = None
    scale_summary: dict[str, Any] | None = None
    if include_early_tp:
        barrier_1r_summary = _summarize_exit_rows(
            [e.barrier_1r or {} for e in fired],
            clear_hurdle_bps=clear_hurdle_bps,
            extra={
                "tp_atr": float(early_tp_atr),
                "sl_atr": DEFAULT_BARRIER_SL_ATR,
                "timeout_seconds": float(barrier_timeout_seconds),
            },
        )
        scale_summary = _summarize_exit_rows(
            [e.scale or {} for e in fired],
            clear_hurdle_bps=clear_hurdle_bps,
            extra={
                "scale_atr": float(early_tp_atr),
                "tp_atr": DEFAULT_BARRIER_TP_ATR,
                "scale_fraction": float(scale_fraction),
                "timeout_seconds": float(barrier_timeout_seconds),
            },
        )

    return {
        "entry_bar": str(entry_bar),
        "variant": variant,
        "require_4h": bool(require_4h),
        "macd_lag_bps": float(macd_lag_bps),
        "max_extension_atr": float(max_extension_atr),
        "pullback": bool(pullback),
        "rsi_pullback_long_max": (
            float(rsi_pullback_long_max) if rsi_pullback_long_max is not None else None
        ),
        "rsi_pullback_short_min": (
            float(rsi_pullback_short_min) if rsi_pullback_short_min is not None else None
        ),
        "decision_ts_min": decision_ts_min,
        "decision_ts_max": decision_ts_max,
        "cooldown_seconds": int(cooldown_seconds),
        "n_steps": n_steps,
        "full_gate_count": n_full_gate,
        "full_gate_rate": (n_full_gate / n_steps) if n_steps else 0.0,
        "cooldown_suppressed": n_suppressed,
        "by_action": dict(by_action),
        "by_regime": {k: int(v) for k, v in sorted(by_regime.items()) if k},
        "by_instrument": by_inst,
        "markout": {
            "fee_model": {
                "source": "fallback",
                "role": "taker",
                "open_fee_bps": float(open_fee_bps),
                "round_trip_fee_bps": 2.0 * float(open_fee_bps),
                "funding_note": "Funding ignored in F0b candle backtest.",
            },
            "by_horizon": markout_by_h,
            "primary_horizon_seconds": int(clear_horizon_seconds),
            "avg_net_rt_bps_5m": primary.get("avg_net_rt_bps"),
            "win_rate_net_rt_5m": primary.get("win_rate_net_rt"),
            "frac_clear_10bps_5m": primary.get("frac_clear_hurdle"),
            "barrier": barrier_summary,
            "trail": trail_summary,
            "mfe": mfe_summary,
            "barrier_1r": barrier_1r_summary,
            "scale": scale_summary,
        },
        "entries": [
            {
                "inst_id": e.inst_id,
                "action": e.action,
                "entry_ts": e.entry_ts,
                "entry_price": e.entry_price,
                "atr_14": e.atr_14,
                "suppressed_cooldown": e.suppressed_cooldown,
                "require_4h_trend": (e.signal_diag or {}).get("require_4h_trend"),
                "trend_gate": (e.signal_diag or {}).get("trend_gate"),
                "regime": (e.signal_diag or {}).get("regime"),
                "markouts": e.markouts,
                "barrier": e.barrier,
                "trail": e.trail,
                "mfe": e.mfe,
                "barrier_1r": e.barrier_1r,
                "scale": e.scale,
            }
            for e in entries
            if not e.suppressed_cooldown
        ],
        "note_vs_live_post_e31": (
            "Live post_e31 markout sample is tiny (arming anecdote n≈4 / "
            "insufficient_post_e31_full_gate_sample). This offline walk expands "
            "n_steps on public candles under the same TF+require_4h+E2B+cooldown "
            "rules; compare fire rate + 5m netRT, not tick-level path."
        ),
    }


__all__ = [
    "BAR_15M_SECONDS",
    "BacktestEntry",
    "DEFAULT_BARRIER_SL_ATR",
    "DEFAULT_BARRIER_TIMEOUT_SECONDS",
    "DEFAULT_HARD_TIME_STOP_SECONDS",
    "DEFAULT_BARRIER_TP_ATR",
    "DEFAULT_EARLY_TP_ATR",
    "DEFAULT_MFE_LEVELS_R",
    "DEFAULT_MFE_TIMEOUT_SECONDS",
    "DEFAULT_SCALE_FRACTION",
    "DEFAULT_TRAIL_ATR",
    "DEFAULT_TRAIL_INITIAL_SL_ATR",
    "DEFAULT_TRAIL_SL_ATR",
    "DEFAULT_TRAIL_TIMEOUT_SECONDS",
    "DEFAULT_COOLDOWN_SECONDS",
    "DEFAULT_LOOKBACK_BARS",
    "HistorySeries",
    "barrier_exit_markout",
    "trail_exit_markout",
    "excursion_markout",
    "scale_exit_markout",
    "trailing_supertrend_exit_markout",
    "build_snapshot_at",
    "fee_aware_markouts",
    "forced_e31_rule_env",
    "price_at_horizon",
    "reprice_exit_rows",
    "rows_to_series",
    "walk_forward_backtest",
]
