"""
Pure function technical indicators.

All functions are:
- Pure: same inputs always produce same outputs
- Side-effect free: no I/O, no state mutation
- Testable: can be unit tested offline

Honest naming: no "quantum", no "causal calculus" marketing.
These are standard technical analysis indicators.
"""
from __future__ import annotations

from dataclasses import dataclass


def calculate_ema(prices: list[float], period: int) -> float:
    """
    Calculate Exponential Moving Average.
    
    Args:
        prices: List of prices (oldest to newest)
        period: EMA period
        
    Returns:
        EMA value, or last price if insufficient data
    """
    if not prices:
        return 0.0
    if len(prices) < period:
        return prices[-1]

    k = 2.0 / (period + 1)
    ema = prices[0]
    for price in prices[1:]:
        ema = price * k + ema * (1 - k)
    return ema


def calculate_sma(prices: list[float], period: int) -> float:
    """
    Calculate Simple Moving Average.
    
    Args:
        prices: List of prices (oldest to newest)
        period: SMA period
        
    Returns:
        SMA value
    """
    if not prices or len(prices) < period:
        return prices[-1] if prices else 0.0
    return sum(prices[-period:]) / period


def calculate_rsi(prices: list[float], period: int = 14) -> float:
    """
    Calculate Relative Strength Index.
    
    Args:
        prices: List of prices (oldest to newest)
        period: RSI period (default 14)
        
    Returns:
        RSI value 0-100, or 50 if insufficient data
    """
    if not prices or len(prices) <= period:
        return 50.0

    gains: list[float] = []
    losses: list[float] = []

    for i in range(1, len(prices)):
        change = prices[i] - prices[i - 1]
        if change >= 0:
            gains.append(change)
            losses.append(0.0)
        else:
            gains.append(0.0)
            losses.append(abs(change))

    if len(gains) < period:
        return 50.0

    avg_gain = sum(gains[:period]) / period
    avg_loss = sum(losses[:period]) / period

    for i in range(period, len(gains)):
        avg_gain = (avg_gain * (period - 1) + gains[i]) / period
        avg_loss = (avg_loss * (period - 1) + losses[i]) / period

    if avg_loss == 0:
        return 100.0

    rs = avg_gain / avg_loss
    return 100.0 - (100.0 / (1.0 + rs))


def calculate_atr(
    highs: list[float],
    lows: list[float],
    closes: list[float],
    period: int = 14,
) -> float:
    """
    Calculate Average True Range.
    
    Args:
        highs: List of high prices (oldest to newest)
        lows: List of low prices
        closes: List of close prices
        period: ATR period (default 14)
        
    Returns:
        ATR value
    """
    if not highs or len(highs) < 2:
        return 0.0

    true_ranges: list[float] = []
    for i in range(1, len(highs)):
        tr = max(
            highs[i] - lows[i],
            abs(highs[i] - closes[i - 1]),
            abs(lows[i] - closes[i - 1]),
        )
        true_ranges.append(tr)

    if not true_ranges:
        return 0.0

    if len(true_ranges) < period:
        return sum(true_ranges) / len(true_ranges)

    atr = sum(true_ranges[:period]) / period
    for tr in true_ranges[period:]:
        atr = (atr * (period - 1) + tr) / period

    return atr


@dataclass
class MACDResult:
    """MACD calculation result."""
    macd_line: float
    signal_line: float
    histogram: float


def calculate_macd(
    prices: list[float],
    fast: int = 12,
    slow: int = 26,
    signal: int = 9,
) -> MACDResult:
    """
    Calculate MACD (Moving Average Convergence Divergence).
    
    Args:
        prices: List of prices (oldest to newest)
        fast: Fast EMA period (default 12)
        slow: Slow EMA period (default 26)
        signal: Signal line period (default 9)
        
    Returns:
        MACDResult with macd_line, signal_line, histogram
    """
    if len(prices) < slow + signal:
        return MACDResult(0.0, 0.0, 0.0)

    k_fast = 2.0 / (fast + 1)
    k_slow = 2.0 / (slow + 1)
    k_signal = 2.0 / (signal + 1)

    fast_ema = prices[0]
    slow_ema = prices[0]
    macd_series: list[float] = []

    for price in prices:
        fast_ema = price * k_fast + fast_ema * (1 - k_fast)
        slow_ema = price * k_slow + slow_ema * (1 - k_slow)
        macd_series.append(fast_ema - slow_ema)

    signal_ema = macd_series[0]
    for macd_val in macd_series:
        signal_ema = macd_val * k_signal + signal_ema * (1 - k_signal)

    macd_line = macd_series[-1]
    histogram = macd_line - signal_ema

    return MACDResult(macd_line, signal_ema, histogram)


@dataclass
class BollingerResult:
    """Bollinger Bands calculation result."""
    middle: float
    upper: float
    lower: float
    bandwidth: float
    percent_b: float


def calculate_bollinger(
    prices: list[float],
    period: int = 20,
    std_dev: float = 2.0,
) -> BollingerResult:
    """
    Calculate Bollinger Bands.
    
    Args:
        prices: List of prices (oldest to newest)
        period: SMA period (default 20)
        std_dev: Standard deviation multiplier (default 2.0)
        
    Returns:
        BollingerResult with middle, upper, lower, bandwidth, percent_b
    """
    if len(prices) < period:
        price = prices[-1] if prices else 0.0
        return BollingerResult(price, price, price, 0.0, 0.5)

    window = prices[-period:]
    middle = sum(window) / period
    variance = sum((p - middle) ** 2 for p in window) / period
    std = variance ** 0.5

    upper = middle + std_dev * std
    lower = middle - std_dev * std
    bandwidth = ((upper - lower) / middle * 100) if middle > 0 else 0.0

    current = prices[-1]
    if upper != lower:
        percent_b = (current - lower) / (upper - lower)
    else:
        percent_b = 0.5

    return BollingerResult(middle, upper, lower, bandwidth, percent_b)


@dataclass
class KeltnerResult:
    """Keltner Channel (EMA ± ATR × multiplier)."""

    middle: float
    upper: float
    lower: float


DEFAULT_KELTNER_PERIOD = 20
DEFAULT_KELTNER_MULTIPLIER = 1.5
DEFAULT_SHOCK_ATR_MULT = 2.5


def calculate_keltner(
    highs: list[float],
    lows: list[float],
    closes: list[float],
    period: int = DEFAULT_KELTNER_PERIOD,
    multiplier: float = DEFAULT_KELTNER_MULTIPLIER,
) -> KeltnerResult:
    """
    Keltner Channel from EMA(close, period) ± multiplier × ATR(period).

    Used with Bollinger Bands for TTM-style squeeze detection.
    """
    if not closes:
        return KeltnerResult(0.0, 0.0, 0.0)
    mid = calculate_ema(closes, max(1, int(period)))
    atr = calculate_atr(highs, lows, closes, max(1, int(period)))
    mult = float(multiplier) if float(multiplier) > 0 else DEFAULT_KELTNER_MULTIPLIER
    return KeltnerResult(
        middle=float(mid),
        upper=float(mid + mult * atr),
        lower=float(mid - mult * atr),
    )


def detect_squeeze(bb: BollingerResult, kc: KeltnerResult) -> bool:
    """True when Bollinger Bands sit inside the Keltner Channel (TTM squeeze)."""
    if bb.upper <= bb.lower or kc.upper <= kc.lower:
        return False
    return bool(bb.upper < kc.upper and bb.lower > kc.lower)


def detect_squeeze_release(*, squeeze_now: bool, squeeze_prev: bool) -> bool:
    """True on the first expansion bar after a TTM squeeze (prev in, now out)."""
    return bool(squeeze_prev) and not bool(squeeze_now)


def classify_market_regime(
    *,
    squeeze: bool,
    supertrend_direction: int,
    trend_1h: str,
    bar_range: float,
    atr: float,
    shock_atr_mult: float = DEFAULT_SHOCK_ATR_MULT,
) -> str:
    """
    P1 regime: shock | squeeze | trend | range.

    Shock: last bar range ≥ shock_atr_mult × ATR (spike / news bar).
    Squeeze: BB inside Keltner — wait for expansion.
    Trend: Supertrend agrees with 1h EMA-stack trend.
    Range: residual (chop, disagreement, or no Supertrend).
    """
    atr_f = float(atr or 0.0)
    rng = float(bar_range or 0.0)
    mult = float(shock_atr_mult) if float(shock_atr_mult) > 0 else DEFAULT_SHOCK_ATR_MULT
    if atr_f > 0.0 and rng >= mult * atr_f:
        return "shock"
    if squeeze:
        return "squeeze"
    t1h = str(trend_1h or "neutral").strip().lower()
    try:
        st = int(supertrend_direction)
    except (TypeError, ValueError):
        st = 0
    if st > 0 and t1h == "bullish":
        return "trend"
    if st < 0 and t1h == "bearish":
        return "trend"
    return "range"


def calculate_vwap(
    prices: list[float],
    volumes: list[float],
    period: int | None = None,
) -> float:
    """
    Calculate Volume Weighted Average Price.
    
    Args:
        prices: List of prices (oldest to newest)
        volumes: List of volumes
        period: Optional period (default: use all data)
        
    Returns:
        VWAP value
    """
    if not prices or not volumes or len(prices) != len(volumes):
        return prices[-1] if prices else 0.0

    if period is not None and period < len(prices):
        prices = prices[-period:]
        volumes = volumes[-period:]

    total_pv = sum(p * v for p, v in zip(prices, volumes))
    total_v = sum(volumes)

    if total_v == 0:
        return prices[-1]

    return total_pv / total_v


def calculate_obv(prices: list[float], volumes: list[float]) -> float:
    """
    Calculate On-Balance Volume.
    
    Args:
        prices: List of prices (oldest to newest)
        volumes: List of volumes
        
    Returns:
        OBV value (cumulative)
    """
    if not prices or not volumes or len(prices) != len(volumes):
        return 0.0

    obv = 0.0
    for i in range(1, len(prices)):
        if prices[i] > prices[i - 1]:
            obv += volumes[i]
        elif prices[i] < prices[i - 1]:
            obv -= volumes[i]

    return obv


@dataclass
class SupertrendPoint:
    """One Supertrend observation (aligned with a closed bar)."""

    value: float
    direction: int  # +1 bullish, -1 bearish, 0 warming / invalid
    atr: float
    upper: float
    lower: float


@dataclass
class SupertrendResult:
    """Latest Supertrend (TradingView-style ATR trailing stop)."""

    value: float
    direction: int  # +1 bullish, -1 bearish, 0 invalid
    atr: float
    upper: float
    lower: float
    valid: bool


DEFAULT_SUPERTREND_PERIOD = 10
DEFAULT_SUPERTREND_MULTIPLIER = 3.0


def calculate_atr_series(
    highs: list[float],
    lows: list[float],
    closes: list[float],
    period: int = 14,
) -> list[float]:
    """
    Wilder ATR series (oldest → newest), same recursion as ``calculate_atr``.

    Bars before ``period`` use the expanding mean of true range so the
    series is aligned with the input (no NaN). Prefer ``calculate_atr``
    when only the latest value is needed.
    """
    n = min(len(highs), len(lows), len(closes))
    if n < 2 or period < 1:
        return [0.0] * n
    true_ranges: list[float] = [0.0]
    for i in range(1, n):
        true_ranges.append(
            max(
                highs[i] - lows[i],
                abs(highs[i] - closes[i - 1]),
                abs(lows[i] - closes[i - 1]),
            )
        )
    atrs: list[float] = []
    running = 0.0
    for i, tr in enumerate(true_ranges):
        if i == 0:
            atrs.append(0.0)
            continue
        if i < period:
            running += tr
            atrs.append(running / float(i))
        elif i == period:
            running += tr
            atrs.append(running / float(period))
        else:
            prev = atrs[-1]
            atrs.append((prev * (period - 1) + tr) / float(period))
    return atrs


def calculate_supertrend_series(
    highs: list[float],
    lows: list[float],
    closes: list[float],
    period: int = DEFAULT_SUPERTREND_PERIOD,
    multiplier: float = DEFAULT_SUPERTREND_MULTIPLIER,
) -> list[SupertrendPoint]:
    """
    Supertrend / UT Bot ATR trailing-stop series (oldest → newest).

    Bands: ``hl2 ± multiplier * ATR``. Direction **+1** = bullish (line is
    the lower band), **-1** = bearish (upper band). Ratchets only in the
    trade's favor — same geometry as TradingView ``ta.supertrend``.
    """
    n = min(len(highs), len(lows), len(closes))
    if n == 0:
        return []
    period = max(1, int(period))
    mult = float(multiplier)
    if mult <= 0:
        mult = DEFAULT_SUPERTREND_MULTIPLIER
    atrs = calculate_atr_series(highs[:n], lows[:n], closes[:n], period)
    out: list[SupertrendPoint] = []
    lower_prev: float | None = None
    upper_prev: float | None = None
    direction = 0
    for i in range(n):
        hl2 = (float(highs[i]) + float(lows[i])) / 2.0
        atr = float(atrs[i]) if i < len(atrs) else 0.0
        basic_lower = hl2 - mult * atr
        basic_upper = hl2 + mult * atr
        if lower_prev is None or upper_prev is None or atr <= 0:
            lower_band = basic_lower
            upper_band = basic_upper
            if atr <= 0:
                direction = 0
            elif i == 0:
                direction = 1 if float(closes[i]) >= hl2 else -1
            value = lower_band if direction >= 0 else upper_band
            out.append(
                SupertrendPoint(
                    value=float(value),
                    direction=int(direction),
                    atr=atr,
                    upper=float(upper_band),
                    lower=float(lower_band),
                )
            )
            lower_prev, upper_prev = lower_band, upper_band
            continue
        # Ratchet: in an uptrend the lower band never falls while close
        # stays above it; upper band never rises in a downtrend.
        if float(closes[i - 1]) > lower_prev:
            lower_band = max(basic_lower, lower_prev)
        else:
            lower_band = basic_lower
        if float(closes[i - 1]) < upper_prev:
            upper_band = min(basic_upper, upper_prev)
        else:
            upper_band = basic_upper
        prev_dir = direction if direction != 0 else 1
        if prev_dir >= 0:
            direction = -1 if float(closes[i]) < lower_band else 1
        else:
            direction = 1 if float(closes[i]) > upper_band else -1
        value = lower_band if direction > 0 else upper_band
        out.append(
            SupertrendPoint(
                value=float(value),
                direction=int(direction),
                atr=atr,
                upper=float(upper_band),
                lower=float(lower_band),
            )
        )
        lower_prev, upper_prev = lower_band, upper_band
    return out


def calculate_supertrend(
    highs: list[float],
    lows: list[float],
    closes: list[float],
    period: int = DEFAULT_SUPERTREND_PERIOD,
    multiplier: float = DEFAULT_SUPERTREND_MULTIPLIER,
) -> SupertrendResult:
    """Latest Supertrend; ``valid=False`` when the series is too short."""
    series = calculate_supertrend_series(
        highs, lows, closes, period=period, multiplier=multiplier
    )
    period_i = max(1, int(period))
    if len(series) < period_i + 1:
        last = series[-1] if series else SupertrendPoint(0.0, 0, 0.0, 0.0, 0.0)
        return SupertrendResult(
            value=float(last.value),
            direction=0,
            atr=float(last.atr),
            upper=float(last.upper),
            lower=float(last.lower),
            valid=False,
        )
    last = series[-1]
    ok = last.direction != 0 and last.atr > 0
    return SupertrendResult(
        value=float(last.value),
        direction=int(last.direction) if ok else 0,
        atr=float(last.atr),
        upper=float(last.upper),
        lower=float(last.lower),
        valid=ok,
    )


def classify_trend(
    ema_short: float,
    ema_medium: float,
    ema_long: float,
    price: float,
) -> str:
    """
    Classify trend direction based on EMA alignment.
    
    Args:
        ema_short: Short-term EMA (e.g., 9)
        ema_medium: Medium-term EMA (e.g., 21)
        ema_long: Long-term EMA (e.g., 55)
        price: Current price
        
    Returns:
        "bullish", "bearish", or "neutral"
    """
    if price > ema_short > ema_medium > ema_long:
        return "bullish"
    elif price < ema_short < ema_medium < ema_long:
        return "bearish"
    else:
        return "neutral"


def compute_volume_ratio(
    volumes: list[float], *, lookback: int = 20
) -> tuple[float, float]:
    """
    Relative volume vs a trailing window (Rule v3 / enrich semantics).

    ``volume_ratio`` = last_bar_volume / mean(last ``lookback`` bars).
    Also returns ``volume_percentile`` ∈ [0, 100]: empirical rank of the last
    bar within the same window (fraction of bars with volume ≤ last × 100).
    """
    if not volumes:
        return 1.0, 50.0
    lb = max(1, int(lookback))
    window = volumes[-lb:] if len(volumes) >= lb else list(volumes)
    avg_vol = sum(window) / float(len(window))
    last = float(volumes[-1])
    ratio = (last / avg_vol) if avg_vol else 1.0
    pct = 100.0 * sum(1 for v in window if float(v) <= last) / float(len(window))
    return float(ratio), float(pct)
