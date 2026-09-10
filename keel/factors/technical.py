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


@dataclass
class SuperTrendResult:
    """Public SuperTrend-style ATR band flip (concept — not vendor Pine)."""

    direction: int  # +1 bullish / -1 bearish (last bar)
    value: float  # band value used as trailing stop (last bar)
    flipped: bool  # True when direction changed on the last bar
    upper: float  # final upper band (last)
    lower: float  # final lower band (last)


def calculate_supertrend(
    highs: list[float],
    lows: list[float],
    closes: list[float],
    period: int = 10,
    factor: float = 3.0,
) -> SuperTrendResult:
    """
    SuperTrend-style direction from ATR bands (oldest → newest).

    Public TA concept: mid = (high+low)/2; basic bands = mid ± factor×ATR;
    final bands ratchet; direction flips when close crosses the prior final
    opposite band. Entry signal for Keel = ``flipped`` on the last bar.
    """
    n = min(len(highs), len(lows), len(closes))
    if n < max(2, int(period) + 1) or period < 1 or factor <= 0:
        px = float(closes[-1]) if closes else 0.0
        return SuperTrendResult(0, px, False, px, px)

    highs = [float(x) for x in highs[-n:]]
    lows = [float(x) for x in lows[-n:]]
    closes = [float(x) for x in closes[-n:]]

    # True range series aligned to index 1..n-1; ATR[i] uses closes through i.
    trs: list[float] = [0.0]
    for i in range(1, n):
        trs.append(
            max(
                highs[i] - lows[i],
                abs(highs[i] - closes[i - 1]),
                abs(lows[i] - closes[i - 1]),
            )
        )

    atrs: list[float] = [0.0] * n
    if n > period:
        atrs[period] = sum(trs[1 : period + 1]) / float(period)
        for i in range(period + 1, n):
            atrs[i] = (atrs[i - 1] * (period - 1) + trs[i]) / float(period)
    else:
        # Warm-up: expanding mean of available TRs.
        for i in range(1, n):
            atrs[i] = sum(trs[1 : i + 1]) / float(i)

    final_upper = [0.0] * n
    final_lower = [0.0] * n
    direction = [1] * n
    st_val = [0.0] * n

    start = max(1, period)
    for i in range(start, n):
        mid = 0.5 * (highs[i] + lows[i])
        basic_upper = mid + float(factor) * atrs[i]
        basic_lower = mid - float(factor) * atrs[i]
        if i == start:
            final_upper[i] = basic_upper
            final_lower[i] = basic_lower
            direction[i] = 1 if closes[i] >= mid else -1
        else:
            prev_fu = final_upper[i - 1]
            prev_fl = final_lower[i - 1]
            # Ratchet: upper only declines in an uptrend; lower only rises in a downtrend.
            if closes[i - 1] <= prev_fu:
                final_upper[i] = min(basic_upper, prev_fu)
            else:
                final_upper[i] = basic_upper
            if closes[i - 1] >= prev_fl:
                final_lower[i] = max(basic_lower, prev_fl)
            else:
                final_lower[i] = basic_lower

            prev_dir = direction[i - 1]
            if prev_dir == 1:
                direction[i] = -1 if closes[i] < final_lower[i] else 1
            else:
                direction[i] = 1 if closes[i] > final_upper[i] else -1

        st_val[i] = final_lower[i] if direction[i] == 1 else final_upper[i]

    last = n - 1
    flipped = last > start and direction[last] != direction[last - 1]
    return SuperTrendResult(
        direction=int(direction[last]),
        value=float(st_val[last]),
        flipped=bool(flipped),
        upper=float(final_upper[last]),
        lower=float(final_lower[last]),
    )


@dataclass
class DonchianChannel:
    """Prior-bar Donchian channel (no repaint: excludes current bar)."""

    upper: float
    lower: float
    period: int


def donchian_prior_channel(
    highs: list[float],
    lows: list[float],
    period: int = 20,
) -> DonchianChannel | None:
    """
    Prior-N-bar Donchian channel (oldest → newest).

    Uses highs/lows of bars ``[-period-1 : -1]`` (excludes the current bar) so
    a close beyond the channel cannot repaint the channel itself.
    """
    p = int(period)
    if p < 1:
        return None
    # Need period prior bars + current ⇒ length ≥ period + 1.
    if len(highs) < p + 1 or len(lows) < p + 1:
        return None
    prior_highs = [float(x) for x in highs[-(p + 1) : -1]]
    prior_lows = [float(x) for x in lows[-(p + 1) : -1]]
    if len(prior_highs) < p or len(prior_lows) < p:
        return None
    return DonchianChannel(
        upper=max(prior_highs),
        lower=min(prior_lows),
        period=p,
    )


def volume_sma_ratio(volumes: list[float], period: int = 20) -> float:
    """
    Last-bar volume / SMA(volume, period).

    Returns 1.0 when data is insufficient or SMA is 0 (neutral — callers
    should still gate on length if they require a hard pass).
    """
    p = max(1, int(period))
    if not volumes:
        return 1.0
    window = volumes[-p:] if len(volumes) >= p else list(volumes)
    if not window:
        return 1.0
    avg = sum(float(v) for v in window) / float(len(window))
    if avg <= 0.0:
        return 1.0
    return float(volumes[-1]) / float(avg)
