"""
F5: modest grids for TradingView-inspired variants vs trend_follow.

Primary offline score: barrier netRT win + avg_net (fee-aware).
Train-only grid; freeze once; validate once. No peeking.
"""
from __future__ import annotations

from dataclasses import asdict, dataclass
from typing import Any, Sequence

from keel.backtest.okx_history_rule import HistorySeries, walk_forward_backtest
from keel.backtest.train_valid import (
    MIN_TRAIN_AVG_NET_RT_BPS,
    MIN_TRAIN_FG,
    extract_train_metrics,
)
from keel.ledger.shadow_markout import DEFAULT_MARKOUT_HORIZONS_SECONDS


@dataclass(frozen=True)
class TvStrategyConfig:
    """One F5 grid cell."""

    variant: str
    require_4h: bool
    cooldown_seconds: int
    st_atr_length: int | None = None
    st_factor: float | None = None
    donchian_period: int | None = None
    donchian_vol_mult: float | None = None
    # TF baseline cells may carry ext/pullback (ignored by ST/DC).
    max_extension_atr: float = 0.0
    pullback: bool = False

    def label(self) -> str:
        parts = [
            self.variant,
            f"4h={'1' if self.require_4h else '0'}",
            f"cd={self.cooldown_seconds}",
        ]
        if self.variant == "supertrend":
            parts.append(f"st={self.st_atr_length}x{self.st_factor}")
        elif self.variant == "donchian":
            parts.append(f"dc={self.donchian_period}/vol≥{self.donchian_vol_mult}")
        elif self.variant == "trend_follow":
            parts.append(f"ext={self.max_extension_atr}")
            parts.append(f"pb={'1' if self.pullback else '0'}")
        return "|".join(parts)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


def modest_tv_grids() -> dict[str, list[TvStrategyConfig]]:
    """
    Small per-variant grids (train only).

    Keep cell counts modest so E2E finishes in a reasonable time.
    """
    cooldowns = (900, 1800)
    require_opts = (True, False)

    tf: list[TvStrategyConfig] = []
    for req4 in require_opts:
        for cd in cooldowns:
            for ext in (0.0, 1.5):
                tf.append(
                    TvStrategyConfig(
                        variant="trend_follow",
                        require_4h=req4,
                        cooldown_seconds=cd,
                        max_extension_atr=ext,
                        pullback=False,
                    )
                )

    st: list[TvStrategyConfig] = []
    for req4 in require_opts:
        for cd in cooldowns:
            for length, factor in ((10, 3.0), (10, 2.0), (14, 3.0)):
                st.append(
                    TvStrategyConfig(
                        variant="supertrend",
                        require_4h=req4,
                        cooldown_seconds=cd,
                        st_atr_length=length,
                        st_factor=factor,
                    )
                )

    dc: list[TvStrategyConfig] = []
    for req4 in require_opts:
        for cd in cooldowns:
            for period, vol_m in ((20, 1.0), (20, 0.8), (55, 1.0)):
                dc.append(
                    TvStrategyConfig(
                        variant="donchian",
                        require_4h=req4,
                        cooldown_seconds=cd,
                        donchian_period=period,
                        donchian_vol_mult=vol_m,
                    )
                )

    return {
        "trend_follow": tf,
        "supertrend": st,
        "donchian": dc,
    }


def barrier_metrics(summary: dict[str, Any]) -> dict[str, Any]:
    """Primary F5 score from barrier_exit_markout aggregate."""
    mo = summary.get("markout") or {}
    bar = mo.get("barrier") or {}
    return {
        "barrier_n": int(bar.get("n_available") or bar.get("sample_count") or 0),
        "barrier_win_rate_net_rt": bar.get("win_rate_net_rt"),
        "barrier_avg_net_rt_bps": bar.get("avg_net_rt_bps"),
        "barrier_frac_clear": bar.get("frac_clear_hurdle"),
        "barrier_by_exit": bar.get("by_exit_reason"),
    }


def run_tv_config_on_window(
    series_list: Sequence[HistorySeries],
    config: TvStrategyConfig,
    *,
    decision_ts_min: float,
    decision_ts_max: float,
    horizons: Sequence[int] = DEFAULT_MARKOUT_HORIZONS_SECONDS,
    clear_horizon_seconds: int | None = None,
    barrier_timeout_seconds: float | None = None,
    entry_bar: str | None = None,
    confirm_mid_bar: str | None = None,
    confirm_high_bar: str | None = None,
) -> dict[str, Any]:
    """Walk with frozen F5 config; always include barrier markout."""
    kwargs: dict[str, Any] = dict(
        variant=config.variant,
        cooldown_seconds=config.cooldown_seconds,
        require_4h=config.require_4h,
        horizons=horizons,
        include_barrier=True,
        max_extension_atr=float(config.max_extension_atr),
        pullback=bool(config.pullback),
        decision_ts_min=decision_ts_min,
        decision_ts_max=decision_ts_max,
        st_atr_length=config.st_atr_length,
        st_factor=config.st_factor,
        donchian_period=config.donchian_period,
        donchian_vol_mult=config.donchian_vol_mult,
        require_1h=True,
    )
    if clear_horizon_seconds is not None:
        kwargs["clear_horizon_seconds"] = int(clear_horizon_seconds)
    if barrier_timeout_seconds is not None:
        kwargs["barrier_timeout_seconds"] = float(barrier_timeout_seconds)
    if entry_bar is not None:
        kwargs["entry_bar"] = entry_bar
    if confirm_mid_bar is not None:
        kwargs["confirm_mid_bar"] = confirm_mid_bar
    if confirm_high_bar is not None:
        kwargs["confirm_high_bar"] = confirm_high_bar
    return walk_forward_backtest(series_list, **kwargs)


def select_by_barrier(
    ranked_rows: Sequence[dict[str, Any]],
    *,
    min_fg: int = MIN_TRAIN_FG,
    min_avg_net_bps: float = MIN_TRAIN_AVG_NET_RT_BPS,
) -> tuple[dict[str, Any] | None, str]:
    """
    Prefer barrier netRT win among rows with FG≥min_fg and barrier avg_net≥min.
    Else best available by barrier win → avg → FG.
    """

    def _key(row: dict[str, Any]) -> tuple[float, float, int]:
        b = row.get("barrier") or {}
        wr = b.get("barrier_win_rate_net_rt")
        avg = b.get("barrier_avg_net_rt_bps")
        fg = int((row.get("train") or {}).get("full_gate_count") or 0)
        return (
            float(wr) if wr is not None else -1.0,
            float(avg) if avg is not None else -1e9,
            fg,
        )

    eligible: list[dict[str, Any]] = []
    for row in ranked_rows:
        train = row.get("train") or {}
        b = row.get("barrier") or {}
        fg = int(train.get("full_gate_count") or 0)
        avg = b.get("barrier_avg_net_rt_bps")
        if fg >= int(min_fg) and avg is not None and float(avg) >= float(min_avg_net_bps):
            eligible.append(row)

    if eligible:
        best = max(eligible, key=_key)
        return best, (
            f"selected by barrier netRT win among FG≥{min_fg} and "
            f"barrier avg_net≥{min_avg_net_bps}bps (n_eligible={len(eligible)})"
        )

    usable = [r for r in ranked_rows if int((r.get("train") or {}).get("full_gate_count") or 0) > 0]
    if not usable:
        if not ranked_rows:
            return None, "no grid rows"
        best = max(
            ranked_rows,
            key=lambda r: int((r.get("train") or {}).get("full_gate_count") or 0),
        )
        return best, "no usable barrier metrics; chose highest train FG"
    best = max(usable, key=_key)
    return best, (
        f"no config met FG≥{min_fg} + barrier avg_net≥{min_avg_net_bps}bps; "
        f"chose best available by barrier win/avg/FG"
    )


def pack_row(config: TvStrategyConfig, summary: dict[str, Any]) -> dict[str, Any]:
    train = extract_train_metrics(summary)
    bar = barrier_metrics(summary)
    return {
        "config": config.to_dict(),
        "label": config.label(),
        "train": train,
        "barrier": bar,
        "summary_lite": {
            "full_gate_count": train.get("full_gate_count"),
            "full_gate_rate": train.get("full_gate_rate"),
            "n_steps": train.get("n_steps"),
            "win_rate_net_rt_5m": train.get("win_rate_net_rt_5m"),
            "avg_net_rt_bps_5m": train.get("avg_net_rt_bps_5m"),
            **bar,
        },
    }


__all__ = [
    "TvStrategyConfig",
    "barrier_metrics",
    "modest_tv_grids",
    "pack_row",
    "run_tv_config_on_window",
    "select_by_barrier",
]
