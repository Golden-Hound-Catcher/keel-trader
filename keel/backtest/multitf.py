"""
F4: multi-timeframe short-vs-short / long-vs-long horizon alignment.

Jo's point (mandatory): do NOT mix horizons — short entry TF uses short
hold/markout; long entry TF uses long hold/markout (bar multiples of T).

Entry bars (OKX): 5m, 15m, 30m, 1H, 4H.
Same calendar train/valid split for all T (see train_valid.make_train_valid_windows).
"""
from __future__ import annotations

from dataclasses import asdict, dataclass
from typing import Any, Sequence

from keel.backtest.okx_history_rule import HistorySeries
from keel.backtest.train_valid import StrategyConfig
from keel.exchange.okx_public import bar_duration_seconds
from keel.factors.market_data import Candle
from keel.worker.cycle import okx_rows_to_candles

# Canonical entry timeframes for F4 multi-TF protocol.
ENTRY_BARS: tuple[str, ...] = ("5m", "15m", "30m", "1H", "4H")


@dataclass(frozen=True)
class EntryTfHorizonSpec:
    """
    Horizon + confirm mapping for one entry timeframe T.

    ``candles_15m`` / ``candles_1h`` / ``candles_4h`` on HistorySeries remain
    *slots*: entry / mid-confirm / high-confirm (legacy names). Markout and
    barrier horizons are **bar multiples of T**, not fixed wall-clock 300s.
    """

    bar: str
    confirm_mid: str
    confirm_high: str
    primary_bars_min: int
    primary_bars_max: int
    primary_bars: int  # selection / clear / barrier timeout
    secondary_bars: int
    cooldown_bars: tuple[int, ...]

    @property
    def bar_seconds(self) -> int:
        return int(bar_duration_seconds(self.bar))

    @property
    def primary_seconds(self) -> int:
        return int(self.primary_bars) * self.bar_seconds

    @property
    def primary_min_seconds(self) -> int:
        return int(self.primary_bars_min) * self.bar_seconds

    @property
    def primary_max_seconds(self) -> int:
        return int(self.primary_bars_max) * self.bar_seconds

    @property
    def secondary_seconds(self) -> int:
        return int(self.secondary_bars) * self.bar_seconds

    def markout_horizons_seconds(self) -> tuple[int, ...]:
        """Primary-min, primary (selection), secondary — unique sorted."""
        vals = sorted(
            {
                self.primary_min_seconds,
                self.primary_seconds,
                self.secondary_seconds,
            }
        )
        return tuple(vals)

    def cooldown_seconds_grid(self) -> tuple[int, ...]:
        bs = self.bar_seconds
        return tuple(int(b) * bs for b in self.cooldown_bars)

    def to_dict(self) -> dict[str, Any]:
        d = asdict(self)
        d["bar_seconds"] = self.bar_seconds
        d["primary_seconds"] = self.primary_seconds
        d["primary_min_seconds"] = self.primary_min_seconds
        d["primary_max_seconds"] = self.primary_max_seconds
        d["secondary_seconds"] = self.secondary_seconds
        d["markout_horizons_seconds"] = list(self.markout_horizons_seconds())
        d["cooldown_seconds_grid"] = list(self.cooldown_seconds_grid())
        return d


# Declared in code + RUNBOOK. Primary ranges from Jo:
#   5m  → 3–6 bars (15–30m), secondary ~12
#   15m → ~4–8 bars (1–2h)  [NOT fixed 300s]
#   30m → ~4–8 bars
#   1H  → ~4–8 bars (4–8h)
#   4H  → ~3–6 bars (12–24h)
_ENTRY_TF_SPECS: dict[str, EntryTfHorizonSpec] = {
    "5m": EntryTfHorizonSpec(
        bar="5m",
        confirm_mid="15m",
        confirm_high="1H",
        primary_bars_min=3,
        primary_bars_max=6,
        primary_bars=4,
        secondary_bars=12,
        cooldown_bars=(3, 6, 12),
    ),
    "15m": EntryTfHorizonSpec(
        bar="15m",
        confirm_mid="1H",
        confirm_high="4H",
        primary_bars_min=4,
        primary_bars_max=8,
        primary_bars=6,
        secondary_bars=12,
        cooldown_bars=(2, 4, 8),
    ),
    "30m": EntryTfHorizonSpec(
        bar="30m",
        confirm_mid="1H",
        confirm_high="4H",
        primary_bars_min=4,
        primary_bars_max=8,
        primary_bars=6,
        secondary_bars=12,
        cooldown_bars=(2, 4, 8),
    ),
    "1H": EntryTfHorizonSpec(
        bar="1H",
        confirm_mid="4H",
        confirm_high="1D",
        primary_bars_min=4,
        primary_bars_max=8,
        primary_bars=6,
        secondary_bars=12,
        cooldown_bars=(2, 4, 8),
    ),
    "4H": EntryTfHorizonSpec(
        bar="4H",
        confirm_mid="4H",
        confirm_high="1D",
        primary_bars_min=3,
        primary_bars_max=6,
        primary_bars=4,
        secondary_bars=8,
        cooldown_bars=(1, 2, 4),
    ),
}


def entry_tf_spec(bar: str) -> EntryTfHorizonSpec:
    key = str(bar).strip()
    if key not in _ENTRY_TF_SPECS:
        raise ValueError(
            f"unsupported entry bar {bar!r}; expected one of {list(_ENTRY_TF_SPECS)}"
        )
    return _ENTRY_TF_SPECS[key]


def all_entry_tf_specs() -> list[EntryTfHorizonSpec]:
    return [entry_tf_spec(b) for b in ENTRY_BARS]


def horizons_for_entry_bar(bar: str) -> tuple[int, ...]:
    """Markout horizons (seconds) scaled to entry bar T."""
    return entry_tf_spec(bar).markout_horizons_seconds()


def primary_horizon_seconds(bar: str) -> int:
    return entry_tf_spec(bar).primary_seconds


def assert_no_horizon_cross_mix(bar_a: str, bar_b: str) -> None:
    """
    Guard: distinct entry bars must not share the same primary wall-clock
    horizon (would indicate short/long mix-up).
    """
    a = entry_tf_spec(bar_a)
    b = entry_tf_spec(bar_b)
    if a.bar == b.bar:
        return
    if a.primary_seconds == b.primary_seconds:
        raise AssertionError(
            f"horizon cross-mix: {bar_a} and {bar_b} share primary "
            f"{a.primary_seconds}s — short/long must not share wall-clock hold"
        )


def modest_multitf_grid(bar: str) -> list[StrategyConfig]:
    """
    Modest per-T grid (~15 cells): TF × ext × pullback × cooldown_bars,
    plus require_4h=off variants (high-confirm slot). Train-only search.
    """
    spec = entry_tf_spec(bar)
    configs: list[StrategyConfig] = []
    pullbacks: list[tuple[bool, float | None, float | None]] = [
        (False, None, None),
        (True, 52.0, 48.0),
    ]
    extensions = (0.0, 1.5)
    cooldowns = spec.cooldown_seconds_grid()

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

    return configs


def bars_to_fetch_for_windows(
    bar: str,
    *,
    train_days: float = 7.0,
    valid_days: float = 7.0,
    lookback_bars: int = 64,
) -> dict[str, Any]:
    """
    How many OKX bars to request for entry + confirm TFs covering
    train+valid calendar span plus lookback and secondary markout tail.
    """
    spec = entry_tf_spec(bar)
    span_s = (float(train_days) + float(valid_days)) * 86400.0
    # Extra: lookback before train_start + secondary markout after last decision.
    extra_s = (
        int(lookback_bars) * spec.bar_seconds
        + spec.secondary_seconds
        + 2 * spec.bar_seconds
    )
    entry_n = int(span_s / spec.bar_seconds) + int(extra_s / spec.bar_seconds) + 40

    def _n_for(confirm_bar: str) -> int:
        cs = bar_duration_seconds(confirm_bar)
        return max(80, int(span_s / cs) + int(lookback_bars) + 40)

    return {
        "entry": max(120, entry_n),
        "mid": _n_for(spec.confirm_mid),
        "high": _n_for(spec.confirm_high),
        "entry_bar": spec.bar,
        "mid_bar": spec.confirm_mid,
        "high_bar": spec.confirm_high,
    }


def _filter_candles_open_before(
    candles: Sequence[Candle], *, open_ts_max_exclusive: float
) -> list[Candle]:
    return [c for c in candles if float(c.timestamp) < float(open_ts_max_exclusive)]


def series_for_train_markout_multitf(
    series: HistorySeries, *, train_end_ts: float
) -> HistorySeries:
    """
    Truncate all TF slots so train markouts cannot see valid-window prices.

    Keep bars with open < train_end_ts (same half-open rule as F3).
    """
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


def rows_to_entry_series(
    inst_id: str,
    rows_entry: Sequence[Sequence[float]],
    rows_mid: Sequence[Sequence[float]],
    rows_high: Sequence[Sequence[float]],
    *,
    entry_bar: str,
) -> HistorySeries:
    """
    Build HistorySeries for entry bar T.

    Slots: entry → candles_15m, mid-confirm → candles_1h, high → candles_4h.
    """
    spec = entry_tf_spec(entry_bar)
    return HistorySeries(
        inst_id=inst_id,
        candles_15m=okx_rows_to_candles([list(r) for r in rows_entry]),
        candles_1h=okx_rows_to_candles([list(r) for r in rows_mid]),
        candles_4h=okx_rows_to_candles([list(r) for r in rows_high]),
        entry_bar=spec.bar,
    )


def windows_do_not_overlap(
    *,
    train_start: float,
    train_end: float,
    valid_start: float,
    valid_end: float,
) -> bool:
    """Half-open [train_start, train_end) and [valid_start, valid_end) disjoint."""
    if train_end != valid_start:
        # Adjacent split is required by Jo protocol; still check no overlap.
        pass
    # Overlap iff intervals intersect as half-open.
    return not (train_start < valid_end and valid_start < train_end and train_start < train_end)


__all__ = [
    "ENTRY_BARS",
    "EntryTfHorizonSpec",
    "all_entry_tf_specs",
    "assert_no_horizon_cross_mix",
    "bars_to_fetch_for_windows",
    "entry_tf_spec",
    "horizons_for_entry_bar",
    "modest_multitf_grid",
    "primary_horizon_seconds",
    "rows_to_entry_series",
    "series_for_train_markout_multitf",
    "windows_do_not_overlap",
]
