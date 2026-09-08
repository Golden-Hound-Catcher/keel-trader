"""
F0b / F2c: offline OKX historical candle backtest under E3.1 rule semantics.

Walks closed 15m bars, builds worker-like snapshots (15m/1H/4H + enrich),
runs ``diagnose_rule_signal`` / ``rule_based_decision`` under forced TF+E2B+E3.1
env (ext=0, pullback=0), applies per-instrument fire cooldown, and fee-aware
markout on future 15m closes (taker RT ~10 bps hurdle).

F2c adds optional ATR barrier exit markout (TP 2.2×ATR / SL 1.0×ATR on
subsequent 15m OHLC path, else timeout 900s) for strategy comparison vs
fixed-horizon 300s markout.

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
    REGULAR_USDT_SWAP_TAKER_BPS,
)
from keel.exchange.okx_public import bar_duration_seconds
from keel.factors.market_data import Candle, MarketSnapshot
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

# Forced E3.1 / E2B / F2a/F2b-off env keys (in-process only — never writes .env).
_ENV_TF_REQUIRE_4H = "KEEL_RULE_TF_REQUIRE_4H"
_ENV_TF_MACD_LAG = "KEEL_RULE_TF_MACD_LAG_BPS"
_ENV_TF_MAX_EXT = "KEEL_RULE_TF_MAX_EXTENSION_ATR"
_ENV_TF_PULLBACK = "KEEL_RULE_TF_PULLBACK"


@dataclass
class HistorySeries:
    """Aligned multi-TF candle history for one instrument (oldest→newest)."""

    inst_id: str
    candles_15m: list[Candle]
    candles_1h: list[Candle]
    candles_4h: list[Candle]


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
) -> MarketSnapshot:
    """
    Build + enrich a MarketSnapshot ending at closed 15m bar ``index_15m``.

    Uses only history available at that bar's close (15m window + closed 1H/4H).
    """
    c15_all = series.candles_15m
    if index_15m < 0 or index_15m >= len(c15_all):
        raise IndexError(f"index_15m out of range: {index_15m}")
    start = max(0, index_15m + 1 - max(1, int(lookback)))
    c15 = list(c15_all[start : index_15m + 1])
    bar_open = float(c15[-1].timestamp)
    decision_ts = bar_open + float(BAR_15M_SECONDS)
    c1h = _closed_higher_tf(series.candles_1h, decision_ts=decision_ts, bar="1H")
    c4h = _closed_higher_tf(series.candles_4h, decision_ts=decision_ts, bar="4H")
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
) -> tuple[float, float, str] | None:
    """
    Price at ``entry_ts + horizon`` from 15m closes.

    Prefers linear interpolation between adjacent bar closes (mid-path proxy).
    Falls back to first close at/after target. Returns
    ``(price_ts, price, source)`` or None.
    """
    if not candles_15m:
        return None
    target = float(entry_ts) + max(0.0, float(horizon_seconds))
    # Close times for each bar.
    closes_meta: list[tuple[float, float]] = []
    for c in candles_15m:
        close_ts = float(c.timestamp) + float(BAR_15M_SECONDS)
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
) -> dict[int, dict[str, Any]]:
    """Per-horizon gross + net open / net RT markout dicts (funding ignored)."""
    rt = float(rt_fee_bps) if rt_fee_bps is not None else 2.0 * float(open_fee_bps)
    out: dict[int, dict[str, Any]] = {}
    for h in horizons:
        hi = int(h)
        found = price_at_horizon(
            candles_15m, entry_ts=entry_ts, horizon_seconds=float(hi)
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
) -> dict[str, Any]:
    """
    Fee-aware ATR barrier exit on subsequent 15m OHLC path (F2c measurement).

    Long: TP = entry + tp_atr×ATR, SL = entry − sl_atr×ATR.
    Short: mirrored. Walks bars with open ≥ entry_ts; if both TP and SL print
    in the same bar, assume SL first (conservative). Else exit at timeout
    (``timeout_seconds``, default 900) via ``price_at_horizon``.
    """
    rt = float(rt_fee_bps) if rt_fee_bps is not None else 2.0 * float(open_fee_bps)
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
        bar_close_ts = bar_open + float(BAR_15M_SECONDS)
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


@contextmanager
def forced_e31_rule_env(
    *,
    variant: str = "trend_follow",
    require_4h: bool = True,
    macd_lag_bps: float = 3.0,
    max_extension_atr: float = 0.0,
    pullback: bool = False,
) -> Iterator[str]:
    """Force TF + E3.1 require_4h + E2B MACD lag; pin F2a/F2b off by default."""
    keys = {
        _ENV_TF_REQUIRE_4H: "1" if require_4h else "0",
        _ENV_TF_MACD_LAG: str(float(macd_lag_bps)),
        _ENV_TF_MAX_EXT: str(float(max_extension_atr)),
        _ENV_TF_PULLBACK: "1" if pullback else "0",
    }
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
) -> dict[str, Any]:
    """
    Walk closed 15m bars per instrument; record full-gate entries + markouts.

    Cooldown: after a full-gate fire, suppress another fire for the same
    ``inst_id`` until ``entry_ts + cooldown_seconds`` (live fire_cooldown
    semantics, in-memory).
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
    ):
        for series in series_list:
            inst = series.inst_id
            by_inst_action.setdefault(inst, Counter())
            n = len(series.candles_15m)
            # Need lookback window; leave room for longest markout (~900s = 1 bar).
            max_h = max(int(h) for h in horizons) if horizons else 0
            # Last index we can decide on: need future path for markout optional;
            # still count steps even if markout missing.
            start_i = max(lookback - 1, 20)
            end_i = n - 1
            for i in range(start_i, end_i + 1):
                snap = build_snapshot_at(series, index_15m=i, lookback=lookback)
                if not snap.data_valid:
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
                )
                # Drop markouts that need future beyond series end.
                for h, mo in list(mos.items()):
                    if not mo.get("available"):
                        # Try: if horizon close would be after last bar, mark missing.
                        target = entry_ts + float(h)
                        last_close_ts = (
                            float(series.candles_15m[-1].timestamp) + BAR_15M_SECONDS
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
                    )
                )

    fired = [e for e in entries if not e.suppressed_cooldown]
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
        max_extension_atr=float(max_extension_atr),
        pullback=bool(pullback),
    )


def _avg(vals: list[float]) -> float | None:
    if not vals:
        return None
    return float(sum(vals) / len(vals))


def _win_rate(vals: list[float]) -> float | None:
    if not vals:
        return None
    return sum(1 for v in vals if v > 0) / len(vals)


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
    max_extension_atr: float = 0.0,
    pullback: bool = False,
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
            "timeout_seconds": DEFAULT_BARRIER_TIMEOUT_SECONDS,
            "vs_fixed_horizon_seconds": int(clear_horizon_seconds),
        }

    return {
        "variant": variant,
        "require_4h": bool(require_4h),
        "macd_lag_bps": float(macd_lag_bps),
        "max_extension_atr": float(max_extension_atr),
        "pullback": bool(pullback),
        "cooldown_seconds": int(cooldown_seconds),
        "n_steps": n_steps,
        "full_gate_count": n_full_gate,
        "full_gate_rate": (n_full_gate / n_steps) if n_steps else 0.0,
        "cooldown_suppressed": n_suppressed,
        "by_action": dict(by_action),
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
                "markouts": e.markouts,
                "barrier": e.barrier,
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
    "DEFAULT_BARRIER_TP_ATR",
    "DEFAULT_COOLDOWN_SECONDS",
    "DEFAULT_LOOKBACK_BARS",
    "HistorySeries",
    "barrier_exit_markout",
    "build_snapshot_at",
    "fee_aware_markouts",
    "forced_e31_rule_env",
    "price_at_horizon",
    "rows_to_series",
    "walk_forward_backtest",
]
