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
