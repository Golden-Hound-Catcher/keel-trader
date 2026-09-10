"""
F6: exit (ATR trail) + ADX regime + SuperTrend soft-entry compare grids.

Primary offline score: trail netRT when include_trail, else barrier netRT.
Train-only grid; freeze once; validate once. No peeking. Live defaults unchanged
(ADX off, ST flip, no trail on live path).
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
class F6StrategyConfig:
    """One F6 grid cell (entry variant + optional ADX + trail exit)."""

    variant: str
    require_4h: bool
    cooldown_seconds: int
    adx_min: float = 0.0
    st_atr_length: int | None = None
    st_factor: float | None = None
    st_entry_mode: str = "flip"
    donchian_period: int | None = None
    donchian_vol_mult: float | None = None
    max_extension_atr: float = 0.0
    pullback: bool = False
    trail_atr: float = 1.5
    trail_time_stop_bars: int | None = None

    def label(self) -> str:
        parts = [
            self.variant,
            f"4h={'1' if self.require_4h else '0'}",
            f"cd={self.cooldown_seconds}",
            f"adx>={self.adx_min:g}" if self.adx_min > 0 else "adx=off",
            f"trail={self.trail_atr:g}",
        ]
        if self.trail_time_stop_bars is not None:
            parts.append(f"tsb={self.trail_time_stop_bars}")
        if self.variant == "supertrend":
            parts.append(f"st={self.st_atr_length}x{self.st_factor}/{self.st_entry_mode}")
        elif self.variant == "donchian":
            parts.append(f"dc={self.donchian_period}/vol≥{self.donchian_vol_mult}")
        elif self.variant == "trend_follow":
            parts.append(f"ext={self.max_extension_atr}")
        return "|".join(parts)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


def modest_f6_grids() -> dict[str, list[F6StrategyConfig]]:
    """
    Small BTC train grids — exit/regime focus, not more entry families.

    Keep cell counts modest so E2E finishes in a reasonable time (~F5 scale).
    """
    # TF: ADX on/off × trail shapes × require_4h (cd fixed 900 for size).
    tf: list[F6StrategyConfig] = []
    for req4 in (True, False):
        for adx in (0.0, 20.0, 25.0):
            for trail_k, tsb in ((1.5, None), (2.0, 12), (1.5, 8)):
                tf.append(
                    F6StrategyConfig(
                        variant="trend_follow",
                        require_4h=req4,
                        cooldown_seconds=900,
                        adx_min=adx,
                        max_extension_atr=0.0,
                        trail_atr=trail_k,
                        trail_time_stop_bars=tsb,
                    )
                )

    # ST: soft is the F6 focus (tiny-n on F5 flip); thin flip baseline.
    st: list[F6StrategyConfig] = []
    for mode in ("soft", "flip"):
        for req4 in (True, False):
            adx_opts = (0.0, 20.0) if mode == "soft" else (0.0,)
            for adx in adx_opts:
                for length, factor in ((10, 3.0), (10, 2.0)):
                    for trail_k, tsb in ((1.5, None), (2.0, 12)):
                        st.append(
                            F6StrategyConfig(
                                variant="supertrend",
                                require_4h=req4,
                                cooldown_seconds=1800 if mode == "soft" else 900,
                                adx_min=adx,
                                st_atr_length=length,
                                st_factor=factor,
                                st_entry_mode=mode,
                                trail_atr=trail_k,
                                trail_time_stop_bars=tsb,
                            )
                        )

    # DC: ADX + trail only (entry family unchanged).
    dc: list[F6StrategyConfig] = []
    for req4 in (True, False):
        for adx in (0.0, 20.0, 25.0):
            for period, vol_m in ((20, 1.0), (20, 0.8)):
                for trail_k, tsb in ((1.5, None), (2.0, 12)):
                    dc.append(
                        F6StrategyConfig(
                            variant="donchian",
                            require_4h=req4,
                            cooldown_seconds=900,
                            adx_min=adx,
                            donchian_period=period,
                            donchian_vol_mult=vol_m,
                            trail_atr=trail_k,
                            trail_time_stop_bars=tsb,
                        )
                    )

    return {
        "trend_follow": tf,
        "supertrend": st,
        "donchian": dc,
    }


def _exit_metrics(summary: dict[str, Any], key: str) -> dict[str, Any]:
    mo = summary.get("markout") or {}
    block = mo.get(key) or {}
    prefix = "trail" if key == "trail" else "barrier"
    return {
        f"{prefix}_n": int(block.get("n_available") or block.get("sample_count") or 0),
        f"{prefix}_win_rate_net_rt": block.get("win_rate_net_rt"),
        f"{prefix}_avg_net_rt_bps": block.get("avg_net_rt_bps"),
        f"{prefix}_frac_clear": block.get("frac_clear_hurdle"),
        f"{prefix}_by_exit": block.get("by_exit_reason"),
    }


def barrier_metrics(summary: dict[str, Any]) -> dict[str, Any]:
    return _exit_metrics(summary, "barrier")


def trail_metrics(summary: dict[str, Any]) -> dict[str, Any]:
    return _exit_metrics(summary, "trail")


def primary_exit_metrics(summary: dict[str, Any]) -> dict[str, Any]:
    """Prefer trail when present with n>0; else barrier."""
    t = trail_metrics(summary)
    if int(t.get("trail_n") or 0) > 0:
        return {
            "primary": "trail",
            "n": t["trail_n"],
            "win_rate_net_rt": t.get("trail_win_rate_net_rt"),
            "avg_net_rt_bps": t.get("trail_avg_net_rt_bps"),
            **t,
            **barrier_metrics(summary),
        }
    b = barrier_metrics(summary)
    return {
        "primary": "barrier",
        "n": b.get("barrier_n") or 0,
        "win_rate_net_rt": b.get("barrier_win_rate_net_rt"),
        "avg_net_rt_bps": b.get("barrier_avg_net_rt_bps"),
        **b,
        **t,
    }


def run_f6_config_on_window(
    series_list: Sequence[HistorySeries],
    config: F6StrategyConfig,
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
    """Walk with frozen F6 config; barrier + trail markout always on."""
    kwargs: dict[str, Any] = dict(
        variant=config.variant,
        cooldown_seconds=config.cooldown_seconds,
        require_4h=config.require_4h,
        horizons=horizons,
        include_barrier=True,
        include_trail=True,
        trail_atr=float(config.trail_atr),
        trail_time_stop_bars=config.trail_time_stop_bars,
        max_extension_atr=float(config.max_extension_atr),
        pullback=bool(config.pullback),
        decision_ts_min=decision_ts_min,
        decision_ts_max=decision_ts_max,
        st_atr_length=config.st_atr_length,
        st_factor=config.st_factor,
        st_entry_mode=config.st_entry_mode,
        donchian_period=config.donchian_period,
        donchian_vol_mult=config.donchian_vol_mult,
        adx_min=float(config.adx_min),
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


def select_by_primary_exit(
    ranked_rows: Sequence[dict[str, Any]],
    *,
    min_fg: int = MIN_TRAIN_FG,
    min_avg_net_bps: float = MIN_TRAIN_AVG_NET_RT_BPS,
) -> tuple[dict[str, Any] | None, str]:
    """Prefer primary (trail|barrier) netRT win among FG/avg floors."""

    def _key(row: dict[str, Any]) -> tuple[float, float, int]:
        p = row.get("primary_exit") or {}
        wr = p.get("win_rate_net_rt")
        avg = p.get("avg_net_rt_bps")
        fg = int((row.get("train") or {}).get("full_gate_count") or 0)
        return (
            float(wr) if wr is not None else -1.0,
            float(avg) if avg is not None else -1e9,
            fg,
        )

    eligible: list[dict[str, Any]] = []
    for row in ranked_rows:
        train = row.get("train") or {}
        p = row.get("primary_exit") or {}
        fg = int(train.get("full_gate_count") or 0)
        avg = p.get("avg_net_rt_bps")
        if fg >= int(min_fg) and avg is not None and float(avg) >= float(min_avg_net_bps):
            eligible.append(row)

    if eligible:
        best = max(eligible, key=_key)
        return best, (
            f"selected by primary exit netRT win among FG≥{min_fg} and "
            f"avg_net≥{min_avg_net_bps}bps (n_eligible={len(eligible)})"
        )

    usable = [
        r for r in ranked_rows if int((r.get("train") or {}).get("full_gate_count") or 0) > 0
    ]
    if not usable:
        if not ranked_rows:
            return None, "no grid rows"
        best = max(
            ranked_rows,
            key=lambda r: int((r.get("train") or {}).get("full_gate_count") or 0),
        )
        return best, "no usable exit metrics; chose highest train FG"
    best = max(usable, key=_key)
    return best, (
        f"no config met FG≥{min_fg} + avg_net≥{min_avg_net_bps}bps; "
        f"chose best available by primary exit win/avg/FG"
    )


def pack_row(config: F6StrategyConfig, summary: dict[str, Any]) -> dict[str, Any]:
    train = extract_train_metrics(summary)
    primary = primary_exit_metrics(summary)
    return {
        "config": config.to_dict(),
        "label": config.label(),
        "train": train,
        "primary_exit": primary,
        "barrier": barrier_metrics(summary),
        "trail": trail_metrics(summary),
        "summary_lite": {
            "full_gate_count": train.get("full_gate_count"),
            "full_gate_rate": train.get("full_gate_rate"),
            "n_steps": train.get("n_steps"),
            "win_rate_net_rt_5m": train.get("win_rate_net_rt_5m"),
            "avg_net_rt_bps_5m": train.get("avg_net_rt_bps_5m"),
            "primary": primary.get("primary"),
            "primary_win": primary.get("win_rate_net_rt"),
            "primary_avg": primary.get("avg_net_rt_bps"),
            "primary_n": primary.get("n"),
        },
    }


__all__ = [
    "F6StrategyConfig",
    "barrier_metrics",
    "modest_f6_grids",
    "pack_row",
    "primary_exit_metrics",
    "run_f6_config_on_window",
    "select_by_primary_exit",
    "trail_metrics",
]
