"""
F3: train / validation split + honest grid selection for OKX history rules.

Jo's protocol (no peeking):
  - Analysis (train): closed-bar decisions in [-14d, -7d)
  - Validation (test): closed-bar decisions in [-7d, now)
  - Fit / rank ONLY on train metrics; freeze ONE config; score once on valid.
"""
from __future__ import annotations

from dataclasses import asdict, dataclass
from typing import Any, Sequence

from keel.backtest.okx_history_rule import (
    BAR_15M_SECONDS,
    HistorySeries,
    walk_forward_backtest,
)
from keel.factors.market_data import Candle
from keel.ledger.shadow_markout import DEFAULT_MARKOUT_HORIZONS_SECONDS

# Selection gates (pre-declared).
MIN_TRAIN_FG = 10
MIN_TRAIN_AVG_NET_RT_BPS = -5.0
TARGET_VALID_WIN = 0.55

# F0b baseline frozen params (compare on valid only).
F0B_BASELINE = {
    "variant": "trend_follow",
    "require_4h": True,
    "max_extension_atr": 0.0,
    "pullback": False,
    "rsi_pullback_long_max": None,
    "rsi_pullback_short_min": None,
    "cooldown_seconds": 900,
}


@dataclass(frozen=True)
class StrategyConfig:
    """One grid cell (frozen params)."""

    variant: str
    require_4h: bool
    max_extension_atr: float
    pullback: bool
    rsi_pullback_long_max: float | None
    rsi_pullback_short_min: float | None
    cooldown_seconds: int

    def label(self) -> str:
        pb = "off"
        if self.pullback:
            lo = self.rsi_pullback_long_max
            sh = self.rsi_pullback_short_min
            pb = f"on(L≤{lo}/S≥{sh})"
        return (
            f"{self.variant}|4h={'1' if self.require_4h else '0'}|"
            f"ext={self.max_extension_atr}|pb={pb}|cd={self.cooldown_seconds}"
        )

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(frozen=True)
class TrainValidWindows:
    """Half-open decision windows on closed-bar timestamps (Unix seconds)."""

    now_ts: float
    train_start_ts: float  # inclusive
    train_end_ts: float  # exclusive (= valid_start)
    valid_start_ts: float  # inclusive
    valid_end_ts: float  # exclusive (usually now)

    def to_dict(self) -> dict[str, Any]:
        return {
            "now_ts": self.now_ts,
            "train_start_ts": self.train_start_ts,
            "train_end_ts": self.train_end_ts,
            "valid_start_ts": self.valid_start_ts,
            "valid_end_ts": self.valid_end_ts,
            "train_days": (self.train_end_ts - self.train_start_ts) / 86400.0,
            "valid_days": (self.valid_end_ts - self.valid_start_ts) / 86400.0,
        }


def make_train_valid_windows(
    now_ts: float,
    *,
    train_days: float = 7.0,
    valid_days: float = 7.0,
) -> TrainValidWindows:
    """
    Build Jo windows: train = [now-14d, now-7d), valid = [now-7d, now).

    ``train_days`` is the analysis span ending at valid_start; total lookback
    from now is train_days + valid_days (default 14d).
    """
    now = float(now_ts)
    valid_start = now - float(valid_days) * 86400.0
    train_start = valid_start - float(train_days) * 86400.0
    return TrainValidWindows(
        now_ts=now,
        train_start_ts=train_start,
        train_end_ts=valid_start,
        valid_start_ts=valid_start,
        valid_end_ts=now,
    )


def _filter_candles_open_before(
    candles: Sequence[Candle], *, open_ts_max_exclusive: float
) -> list[Candle]:
    """Keep bars whose open time is strictly before ``open_ts_max_exclusive``."""
    out: list[Candle] = []
    for c in candles:
        if float(c.timestamp) < float(open_ts_max_exclusive):
            out.append(c)
    return out


def series_for_train_markout(
    series: HistorySeries, *, train_end_ts: float
) -> HistorySeries:
    """
    Truncate so train markouts cannot see valid-window prices.

    Decision at ``decision_ts`` uses bar open = decision_ts - 900; markout path
    uses subsequent 15m OHLC. Truncate candles with open >= train_end_ts so the
    last usable close is at train_end_ts (bar that closed at train_end).
    """
    # Bar that closes at train_end has open = train_end - 900; keep opens < train_end.
    open_max = float(train_end_ts)
    return HistorySeries(
        inst_id=series.inst_id,
        candles_15m=_filter_candles_open_before(
            series.candles_15m, open_ts_max_exclusive=open_max
        ),
        candles_1h=_filter_candles_open_before(
            series.candles_1h, open_ts_max_exclusive=open_max
        ),
        candles_4h=_filter_candles_open_before(
            series.candles_4h, open_ts_max_exclusive=open_max
        ),
        entry_bar=getattr(series, "entry_bar", "15m") or "15m",
    )


def decision_bounds_ok(
    decision_ts: float,
    *,
    window_start: float,
    window_end: float,
) -> bool:
    """Half-open [start, end) membership for closed-bar decision timestamps."""
    t = float(decision_ts)
    return float(window_start) <= t < float(window_end)


def modest_strategy_grid() -> list[StrategyConfig]:
    """
    Modest honest grid (~40 cells): TF primary × ext × pullback × cooldown,
    plus a few require_4h=off and mean_revert cooldown variants.
    """
    configs: list[StrategyConfig] = []
    pullbacks: list[tuple[bool, float | None, float | None]] = [
        (False, None, None),
        (True, 52.0, 48.0),
        (True, 55.0, 45.0),
    ]
    extensions = (0.0, 1.0, 1.5, 2.0)
    cooldowns = (900, 1800, 3600)

    # Primary: TF + require_4h (E3.1) — 4 × 3 × 3 = 36
    for ext in extensions:
        for pb_on, lo, sh in pullbacks:
            for cd in cooldowns:
                configs.append(
                    StrategyConfig(
                        variant="trend_follow",
                        require_4h=True,
                        max_extension_atr=float(ext),
                        pullback=pb_on,
                        rsi_pullback_long_max=lo,
                        rsi_pullback_short_min=sh,
                        cooldown_seconds=int(cd),
                    )
                )

    # TF without 4h, ext=0, pullback off — 3
    for cd in cooldowns:
        configs.append(
            StrategyConfig(
                variant="trend_follow",
                require_4h=False,
                max_extension_atr=0.0,
                pullback=False,
                rsi_pullback_long_max=None,
                rsi_pullback_short_min=None,
                cooldown_seconds=int(cd),
            )
        )

    # mean_revert (ext/pullback N/A) — 3
    for cd in cooldowns:
        configs.append(
            StrategyConfig(
                variant="mean_revert",
                require_4h=False,
                max_extension_atr=0.0,
                pullback=False,
                rsi_pullback_long_max=None,
                rsi_pullback_short_min=None,
                cooldown_seconds=int(cd),
            )
        )

    return configs


def extract_train_metrics(summary: dict[str, Any]) -> dict[str, Any]:
    """Flatten primary train metrics used for ranking / selection.

    Field names ``*_5m`` are legacy (F0b/F3 clear-horizon label); for F4 the
    primary horizon is bar-scaled via ``clear_horizon_seconds`` — values still
    land in these keys from ``_summarize``.
    """
    mo = summary.get("markout") or {}
    by_h = mo.get("by_horizon") or {}
    primary_s = mo.get("primary_horizon_seconds")
    primary_row = None
    if primary_s is not None:
        primary_row = by_h.get(str(int(primary_s)))
    return {
        "full_gate_count": int(summary.get("full_gate_count") or 0),
        "full_gate_rate": summary.get("full_gate_rate"),
        "n_steps": int(summary.get("n_steps") or 0),
        "win_rate_net_rt_5m": mo.get("win_rate_net_rt_5m"),
        "avg_net_rt_bps_5m": mo.get("avg_net_rt_bps_5m"),
        "frac_clear_10bps_5m": mo.get("frac_clear_10bps_5m"),
        "primary_horizon_seconds": primary_s,
        "primary_markout": primary_row,
        "by_horizon_900": by_h.get("900"),
        "barrier": mo.get("barrier"),
        "entry_bar": summary.get("entry_bar"),
    }


def meets_selection_gates(
    metrics: dict[str, Any],
    *,
    min_fg: int = MIN_TRAIN_FG,
    min_avg_net_bps: float = MIN_TRAIN_AVG_NET_RT_BPS,
) -> bool:
    fg = int(metrics.get("full_gate_count") or 0)
    avg = metrics.get("avg_net_rt_bps_5m")
    if fg < int(min_fg):
        return False
    if avg is None:
        return False
    return float(avg) >= float(min_avg_net_bps)


def select_primary_strategy(
    ranked_rows: Sequence[dict[str, Any]],
    *,
    min_fg: int = MIN_TRAIN_FG,
    min_avg_net_bps: float = MIN_TRAIN_AVG_NET_RT_BPS,
) -> tuple[dict[str, Any] | None, str]:
    """
    Pre-declared rule: max train 5m netRT win among rows with FG≥min_fg and
    avg netRT ≥ min_avg; else best available by (win, avg, fg) and say so.

    ``ranked_rows`` items must include ``config``, ``train`` metrics, and
    preferably already sorted by the same key used for reporting.
    """
    eligible: list[dict[str, Any]] = []
    for row in ranked_rows:
        train = row.get("train") or {}
        if meets_selection_gates(train, min_fg=min_fg, min_avg_net_bps=min_avg_net_bps):
            eligible.append(row)

    def _sort_key(row: dict[str, Any]) -> tuple[float, float, int]:
        t = row.get("train") or {}
        wr = t.get("win_rate_net_rt_5m")
        avg = t.get("avg_net_rt_bps_5m")
        fg = int(t.get("full_gate_count") or 0)
        return (
            float(wr) if wr is not None else -1.0,
            float(avg) if avg is not None else -1e9,
            fg,
        )

    if eligible:
        best = max(eligible, key=_sort_key)
        return best, (
            f"selected by pre-declared rule: max train 5m netRT win among "
            f"FG≥{min_fg} and avg_net≥{min_avg_net_bps}bps "
            f"(n_eligible={len(eligible)})"
        )

    usable = [
        r
        for r in ranked_rows
        if (r.get("train") or {}).get("win_rate_net_rt_5m") is not None
        or int((r.get("train") or {}).get("full_gate_count") or 0) > 0
    ]
    if not usable:
        # Fall back to any row with max FG.
        if not ranked_rows:
            return None, "no grid rows — cannot select"
        best = max(
            ranked_rows,
            key=lambda r: int((r.get("train") or {}).get("full_gate_count") or 0),
        )
        return best, (
            "no config met gates and none had usable 5m netRT; "
            "chose highest train FG (best available)"
        )
    best = max(usable, key=_sort_key)
    return best, (
        f"no config met FG≥{min_fg} and avg_net≥{min_avg_net_bps}bps; "
        f"chose best available by train 5m win then avg net then FG"
    )


def rank_train_rows(rows: Sequence[dict[str, Any]]) -> list[dict[str, Any]]:
    """Sort by train 5m win desc, then avg net, then FG (report order)."""

    def _key(row: dict[str, Any]) -> tuple[float, float, int]:
        t = row.get("train") or {}
        wr = t.get("win_rate_net_rt_5m")
        avg = t.get("avg_net_rt_bps_5m")
        fg = int(t.get("full_gate_count") or 0)
        return (
            float(wr) if wr is not None else -1.0,
            float(avg) if avg is not None else -1e9,
            fg,
        )

    return sorted(rows, key=_key, reverse=True)


def run_config_on_window(
    series_list: Sequence[HistorySeries],
    config: StrategyConfig,
    *,
    decision_ts_min: float,
    decision_ts_max: float,
    include_barrier: bool = False,
    horizons: Sequence[int] = DEFAULT_MARKOUT_HORIZONS_SECONDS,
    clear_horizon_seconds: int | None = None,
    barrier_timeout_seconds: float | None = None,
    entry_bar: str | None = None,
    confirm_mid_bar: str | None = None,
    confirm_high_bar: str | None = None,
) -> dict[str, Any]:
    """Walk with frozen config; decisions only in [min, max).

    F4: pass ``entry_bar`` + bar-scaled ``horizons`` / ``clear_horizon_seconds``.
    """
    kwargs: dict[str, Any] = dict(
        variant=config.variant,
        cooldown_seconds=config.cooldown_seconds,
        require_4h=config.require_4h,
        horizons=horizons,
        include_barrier=include_barrier,
        max_extension_atr=config.max_extension_atr,
        pullback=config.pullback,
        rsi_pullback_long_max=config.rsi_pullback_long_max,
        rsi_pullback_short_min=config.rsi_pullback_short_min,
        decision_ts_min=decision_ts_min,
        decision_ts_max=decision_ts_max,
    )
    if clear_horizon_seconds is not None:
        kwargs["clear_horizon_seconds"] = int(clear_horizon_seconds)
    if barrier_timeout_seconds is not None:
        kwargs["barrier_timeout_seconds"] = float(barrier_timeout_seconds)
    if entry_bar is not None:
        kwargs["entry_bar"] = str(entry_bar)
    if confirm_mid_bar is not None:
        kwargs["confirm_mid_bar"] = str(confirm_mid_bar)
    if confirm_high_bar is not None:
        kwargs["confirm_high_bar"] = str(confirm_high_bar)
    return walk_forward_backtest(series_list, **kwargs)


def config_from_dict(d: dict[str, Any]) -> StrategyConfig:
    return StrategyConfig(
        variant=str(d.get("variant") or "trend_follow"),
        require_4h=bool(d.get("require_4h", True)),
        max_extension_atr=float(d.get("max_extension_atr") or 0.0),
        pullback=bool(d.get("pullback", False)),
        rsi_pullback_long_max=(
            float(d["rsi_pullback_long_max"])
            if d.get("rsi_pullback_long_max") is not None
            else None
        ),
        rsi_pullback_short_min=(
            float(d["rsi_pullback_short_min"])
            if d.get("rsi_pullback_short_min") is not None
            else None
        ),
        cooldown_seconds=int(d.get("cooldown_seconds") or 900),
    )


def f0b_baseline_config() -> StrategyConfig:
    return config_from_dict(F0B_BASELINE)


__all__ = [
    "BAR_15M_SECONDS",
    "F0B_BASELINE",
    "MIN_TRAIN_AVG_NET_RT_BPS",
    "MIN_TRAIN_FG",
    "TARGET_VALID_WIN",
    "StrategyConfig",
    "TrainValidWindows",
    "config_from_dict",
    "decision_bounds_ok",
    "extract_train_metrics",
    "f0b_baseline_config",
    "make_train_valid_windows",
    "meets_selection_gates",
    "modest_strategy_grid",
    "rank_train_rows",
    "run_config_on_window",
    "select_primary_strategy",
    "series_for_train_markout",
]
