import pytest
import time
from core.indicators import compute_indicators, compute_signal_score


def _make_candles(n: int = 100, base_price: float = 100.0, trend: float = 0.001) -> list[dict]:
    """Generate synthetic OHLCV candles."""
    candles = []
    price = base_price
    for i in range(n):
        price *= (1 + trend)
        high = price * 1.005
        low = price * 0.995
        open_ = price * 0.999
        close = price
        candles.append({
            "timestamp": time.time() - (n - i) * 60,
            "token": "TEST",
            "timeframe": "1m",
            "open": open_,
            "high": high,
            "low": low,
            "close": close,
            "volume": 1000 + i * 10,
        })
    return candles


def _default_strategy_config() -> dict:
    return {
        "weights": {"momentum": 30, "trend": 30, "mean_reversion": 20, "volume": 20},
        "momentum": {"lookback_periods": [15, 60]},
        "trend": {"ema_periods": [9, 21, 50], "macd": [12, 26, 9]},
        "mean_reversion": {"rsi_period": 14, "rsi_oversold": 30, "rsi_overbought": 70, "bb_period": 20, "bb_std": 2.0},
        "volume": {"ratio_threshold": 1.5, "lookback_periods": 20},
    }


class TestComputeIndicators:
    def test_returns_empty_for_insufficient_data(self):
        candles = _make_candles(10)
        result = compute_indicators(candles, _default_strategy_config())
        assert result == {}

    def test_returns_indicators_for_sufficient_data(self):
        candles = _make_candles(100)
        result = compute_indicators(candles, _default_strategy_config())
        assert "rsi" in result
        assert "ema_9" in result
        assert "ema_21" in result
        assert "ema_50" in result
        assert "macd_hist" in result
        assert "bb_position" in result
        assert "atr" in result
        assert "volume_ratio" in result
        assert "price" in result

    def test_rsi_in_valid_range(self):
        candles = _make_candles(100)
        result = compute_indicators(candles, _default_strategy_config())
        assert 0 <= result["rsi"] <= 100

    def test_bb_position_in_valid_range(self):
        candles = _make_candles(100)
        result = compute_indicators(candles, _default_strategy_config())
        assert -0.5 <= result["bb_position"] <= 1.5  # Can slightly exceed 0-1

    def test_uptrend_bullish_ema(self):
        candles = _make_candles(100, trend=0.003)  # Strong uptrend
        result = compute_indicators(candles, _default_strategy_config())
        assert result.get("ema_alignment") == "BULLISH"

    def test_downtrend_bearish_ema(self):
        candles = _make_candles(100, trend=-0.003)  # Strong downtrend
        result = compute_indicators(candles, _default_strategy_config())
        assert result.get("ema_alignment") == "BEARISH"


class TestComputeSignalScore:
    def test_score_in_valid_range(self):
        candles = _make_candles(100)
        indicators = compute_indicators(candles, _default_strategy_config())
        score, direction = compute_signal_score(indicators, _default_strategy_config())
        assert 0 <= score <= 100
        assert direction in ("LONG", "SHORT")

    def test_strong_uptrend_gives_long(self):
        candles = _make_candles(100, trend=0.005)
        indicators = compute_indicators(candles, _default_strategy_config())
        score, direction = compute_signal_score(indicators, _default_strategy_config())
        assert direction == "LONG"

    def test_strong_downtrend_gives_short(self):
        candles = _make_candles(100, trend=-0.005)
        indicators = compute_indicators(candles, _default_strategy_config())
        score, direction = compute_signal_score(indicators, _default_strategy_config())
        assert direction == "SHORT"

    def test_empty_indicators_return_zero(self):
        score, direction = compute_signal_score({}, _default_strategy_config())
        assert score == 0
        assert direction in ("LONG", "SHORT")
