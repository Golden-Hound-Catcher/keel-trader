"""
Market data structures for factor calculation.

Pure data classes with no side effects.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Literal


@dataclass(frozen=True)
class Candle:
    """OHLCV candle data."""
    timestamp: float
    open: float
    high: float
    low: float
    close: float
    volume: float

    @property
    def is_bullish(self) -> bool:
        return self.close > self.open

    @property
    def is_bearish(self) -> bool:
        return self.close < self.open

    @property
    def body_size(self) -> float:
        return abs(self.close - self.open)

    @property
    def upper_wick(self) -> float:
        return self.high - max(self.open, self.close)

    @property
    def lower_wick(self) -> float:
        return min(self.open, self.close) - self.low

    @property
    def range(self) -> float:
        return self.high - self.low


@dataclass
class MarketSnapshot:
    """
    Point-in-time market data snapshot for a single instrument.
    
    Contains raw candles and computed factors.
    Factors are computed lazily or by external code.
    """
    inst_id: str
    name: str
    timestamp: float

    # Current price data
    price: float = 0.0
    bid: float = 0.0
    ask: float = 0.0

    # Candle history (newest first)
    candles_15m: list[Candle] = field(default_factory=list)
    candles_1h: list[Candle] = field(default_factory=list)
    candles_4h: list[Candle] = field(default_factory=list)

    # Computed factors (set after calculation)
    ema_9: float = 0.0
    ema_21: float = 0.0
    ema_55: float = 0.0
    rsi_14: float = 50.0
    rsi_7: float = 50.0
    atr_14: float = 0.0
    atr_pct: float = 0.0
    macd_line: float = 0.0
    macd_signal: float = 0.0
    macd_histogram: float = 0.0
    vwap: float = 0.0
    vwap_bias_pct: float = 0.0
    obv: float = 0.0
    volume_ratio: float = 1.0

    # Structure classification
    trend_15m: Literal["bullish", "bearish", "neutral"] = "neutral"
    trend_1h: Literal["bullish", "bearish", "neutral"] = "neutral"
    trend_4h: Literal["bullish", "bearish", "neutral"] = "neutral"

    # Data quality
    data_valid: bool = False
    data_quality_reason: str = ""

    @property
    def spread_pct(self) -> float:
        if self.bid > 0:
            return (self.ask - self.bid) / self.bid * 100
        return 0.0
