"""Tests for keel.factors module - pure function technical indicators."""
import unittest

from keel.factors.technical import (
    calculate_ema,
    calculate_sma,
    calculate_rsi,
    calculate_atr,
    calculate_macd,
    calculate_bollinger,
    calculate_vwap,
    calculate_obv,
    classify_trend,
)


class TestEMA(unittest.TestCase):
    """Tests for EMA calculation."""

    def test_empty_prices(self):
        self.assertEqual(calculate_ema([], 9), 0.0)

    def test_single_price(self):
        self.assertEqual(calculate_ema([100.0], 9), 100.0)

    def test_insufficient_data(self):
        prices = [100.0, 101.0, 102.0]
        result = calculate_ema(prices, 9)
        self.assertEqual(result, prices[-1])

    def test_ema_calculation(self):
        prices = list(range(1, 21))
        ema = calculate_ema(prices, 9)
        self.assertGreater(ema, 0)
        self.assertLess(ema, prices[-1])

    def test_ema_weights_recent(self):
        prices = [100.0] * 20 + [200.0]
        ema = calculate_ema(prices, 9)
        self.assertGreater(ema, 100.0)
        self.assertLess(ema, 200.0)


class TestSMA(unittest.TestCase):
    """Tests for SMA calculation."""

    def test_empty_prices(self):
        self.assertEqual(calculate_sma([], 5), 0.0)

    def test_exact_period(self):
        prices = [10.0, 20.0, 30.0, 40.0, 50.0]
        self.assertEqual(calculate_sma(prices, 5), 30.0)

    def test_longer_than_period(self):
        prices = [10.0, 20.0, 30.0, 40.0, 50.0, 60.0]
        self.assertEqual(calculate_sma(prices, 5), 40.0)


class TestRSI(unittest.TestCase):
    """Tests for RSI calculation."""

    def test_insufficient_data(self):
        self.assertEqual(calculate_rsi([100.0, 101.0], 14), 50.0)

    def test_all_up(self):
        prices = list(range(100, 120))
        rsi = calculate_rsi(prices, 14)
        self.assertEqual(rsi, 100.0)

    def test_all_down(self):
        prices = list(range(120, 100, -1))
        rsi = calculate_rsi(prices, 14)
        self.assertLess(rsi, 10.0)

    def test_neutral(self):
        prices = [100.0, 101.0, 100.0, 101.0, 100.0, 101.0] * 5
        rsi = calculate_rsi(prices, 14)
        self.assertGreater(rsi, 40.0)
        self.assertLess(rsi, 60.0)


class TestATR(unittest.TestCase):
    """Tests for ATR calculation."""

    def test_insufficient_data(self):
        self.assertEqual(calculate_atr([100], [99], [100], 14), 0.0)

    def test_atr_calculation(self):
        highs = [102.0, 103.0, 104.0, 105.0, 106.0] * 4
        lows = [98.0, 99.0, 100.0, 101.0, 102.0] * 4
        closes = [100.0, 101.0, 102.0, 103.0, 104.0] * 4
        
        atr = calculate_atr(highs, lows, closes, 14)
        self.assertGreater(atr, 0)

    def test_atr_increases_with_volatility(self):
        low_vol_highs = [101.0] * 20
        low_vol_lows = [99.0] * 20
        low_vol_closes = [100.0] * 20
        
        high_vol_highs = [110.0] * 20
        high_vol_lows = [90.0] * 20
        high_vol_closes = [100.0] * 20
        
        low_atr = calculate_atr(low_vol_highs, low_vol_lows, low_vol_closes, 14)
        high_atr = calculate_atr(high_vol_highs, high_vol_lows, high_vol_closes, 14)
        
        self.assertGreater(high_atr, low_atr)


class TestMACD(unittest.TestCase):
    """Tests for MACD calculation."""

    def test_insufficient_data(self):
        result = calculate_macd([100.0] * 10)
        self.assertEqual(result.macd_line, 0.0)
        self.assertEqual(result.signal_line, 0.0)
        self.assertEqual(result.histogram, 0.0)

    def test_uptrend_positive_macd(self):
        prices = list(range(100, 150))
        result = calculate_macd(prices)
        self.assertGreater(result.macd_line, 0)

    def test_downtrend_negative_macd(self):
        prices = list(range(150, 100, -1))
        result = calculate_macd(prices)
        self.assertLess(result.macd_line, 0)


class TestBollinger(unittest.TestCase):
    """Tests for Bollinger Bands calculation."""

    def test_insufficient_data(self):
        result = calculate_bollinger([100.0] * 5)
        self.assertEqual(result.middle, 100.0)

    def test_flat_prices(self):
        prices = [100.0] * 25
        result = calculate_bollinger(prices)
        self.assertEqual(result.middle, 100.0)
        self.assertEqual(result.upper, 100.0)
        self.assertEqual(result.lower, 100.0)
        self.assertEqual(result.bandwidth, 0.0)

    def test_volatile_prices_wider_bands(self):
        flat_prices = [100.0] * 25
        volatile_prices = [95.0, 105.0] * 12 + [100.0]
        
        flat_result = calculate_bollinger(flat_prices)
        volatile_result = calculate_bollinger(volatile_prices)
        
        self.assertGreater(volatile_result.bandwidth, flat_result.bandwidth)


class TestVWAP(unittest.TestCase):
    """Tests for VWAP calculation."""

    def test_equal_volumes(self):
        prices = [100.0, 110.0, 120.0]
        volumes = [1.0, 1.0, 1.0]
        vwap = calculate_vwap(prices, volumes)
        self.assertEqual(vwap, 110.0)

    def test_weighted_by_volume(self):
        prices = [100.0, 200.0]
        volumes = [10.0, 1.0]
        vwap = calculate_vwap(prices, volumes)
        self.assertAlmostEqual(vwap, 109.09, places=1)

    def test_zero_volume(self):
        prices = [100.0, 110.0]
        volumes = [0.0, 0.0]
        vwap = calculate_vwap(prices, volumes)
        self.assertEqual(vwap, 110.0)


class TestOBV(unittest.TestCase):
    """Tests for OBV calculation."""

    def test_all_up(self):
        prices = [100.0, 101.0, 102.0, 103.0]
        volumes = [1000.0, 1000.0, 1000.0, 1000.0]
        obv = calculate_obv(prices, volumes)
        self.assertEqual(obv, 3000.0)

    def test_all_down(self):
        prices = [103.0, 102.0, 101.0, 100.0]
        volumes = [1000.0, 1000.0, 1000.0, 1000.0]
        obv = calculate_obv(prices, volumes)
        self.assertEqual(obv, -3000.0)

    def test_flat(self):
        prices = [100.0, 100.0, 100.0]
        volumes = [1000.0, 1000.0, 1000.0]
        obv = calculate_obv(prices, volumes)
        self.assertEqual(obv, 0.0)


class TestClassifyTrend(unittest.TestCase):
    """Tests for trend classification."""

    def test_bullish(self):
        result = classify_trend(ema_short=105, ema_medium=103, ema_long=100, price=107)
        self.assertEqual(result, "bullish")

    def test_bearish(self):
        result = classify_trend(ema_short=95, ema_medium=97, ema_long=100, price=93)
        self.assertEqual(result, "bearish")

    def test_neutral(self):
        result = classify_trend(ema_short=100, ema_medium=100, ema_long=100, price=100)
        self.assertEqual(result, "neutral")


if __name__ == "__main__":
    unittest.main()
