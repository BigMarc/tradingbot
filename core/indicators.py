from __future__ import annotations

import pandas as pd
import pandas_ta as ta
import numpy as np
from utils.logger import logger


def compute_indicators(candles: list[dict], strategy_config: dict) -> dict:
    """Compute all technical indicators from OHLCV candles.

    Returns a dict with all indicator values for signal scoring.
    """
    if len(candles) < 50:
        return {}

    df = pd.DataFrame(candles)
    df.columns = [c.lower() for c in df.columns]

    required = {"open", "high", "low", "close", "volume"}
    if not required.issubset(df.columns):
        return {}

    for col in ["open", "high", "low", "close", "volume"]:
        df[col] = pd.to_numeric(df[col], errors="coerce")

    df.dropna(subset=["close"], inplace=True)
    if len(df) < 50:
        return {}

    result: dict = {}

    # RSI
    rsi_period = strategy_config.get("mean_reversion", {}).get("rsi_period", 14)
    rsi = ta.rsi(df["close"], length=rsi_period)
    if rsi is not None and len(rsi) > 0:
        result["rsi"] = float(rsi.iloc[-1]) if not pd.isna(rsi.iloc[-1]) else 50.0
    else:
        result["rsi"] = 50.0

    # EMAs
    ema_periods = strategy_config.get("trend", {}).get("ema_periods", [9, 21, 50])
    for period in ema_periods:
        ema = ta.ema(df["close"], length=period)
        if ema is not None and len(ema) > 0:
            result[f"ema_{period}"] = float(ema.iloc[-1]) if not pd.isna(ema.iloc[-1]) else float(df["close"].iloc[-1])

    # EMA alignment
    price = float(df["close"].iloc[-1])
    emas = [result.get(f"ema_{p}", price) for p in ema_periods]
    if len(emas) == 3:
        if emas[0] > emas[1] > emas[2]:
            result["ema_alignment"] = "BULLISH"
        elif emas[0] < emas[1] < emas[2]:
            result["ema_alignment"] = "BEARISH"
        else:
            result["ema_alignment"] = "MIXED"

    # MACD
    macd_params = strategy_config.get("trend", {}).get("macd", [12, 26, 9])
    macd_result = ta.macd(df["close"], fast=macd_params[0], slow=macd_params[1], signal=macd_params[2])
    if macd_result is not None and len(macd_result) > 0:
        hist_col = [c for c in macd_result.columns if c.lower().startswith("macdh")]
        signal_col = [c for c in macd_result.columns if c.lower().startswith("macds")]
        macd_col = [c for c in macd_result.columns if c.lower().startswith("macd_") or c.lower() == "macd"]
        if not macd_col:
            macd_col = [c for c in macd_result.columns if c not in hist_col and c not in signal_col]

        if hist_col:
            val = macd_result[hist_col[0]].iloc[-1]
            result["macd_hist"] = float(val) if not pd.isna(val) else 0.0
            # Trend direction from last 3 bars
            hist_vals = macd_result[hist_col[0]].tail(3).dropna()
            if len(hist_vals) >= 2:
                result["macd_trend"] = "RISING" if hist_vals.iloc[-1] > hist_vals.iloc[-2] else "FALLING"
            else:
                result["macd_trend"] = "FLAT"
        if macd_col:
            val = macd_result[macd_col[0]].iloc[-1]
            result["macd_line"] = float(val) if not pd.isna(val) else 0.0

    # Bollinger Bands
    bb_period = strategy_config.get("mean_reversion", {}).get("bb_period", 20)
    bb_std = strategy_config.get("mean_reversion", {}).get("bb_std", 2.0)
    bbands = ta.bbands(df["close"], length=bb_period, std=bb_std)
    if bbands is not None and len(bbands) > 0:
        lower_col = [c for c in bbands.columns if "l" in c.lower() and "b" in c.lower()]
        upper_col = [c for c in bbands.columns if "u" in c.lower() and "b" in c.lower()]
        if lower_col and upper_col:
            lower = float(bbands[lower_col[0]].iloc[-1]) if not pd.isna(bbands[lower_col[0]].iloc[-1]) else price
            upper = float(bbands[upper_col[0]].iloc[-1]) if not pd.isna(bbands[upper_col[0]].iloc[-1]) else price
            if upper > lower and np.isfinite(upper) and np.isfinite(lower):
                bb_pos = (price - lower) / (upper - lower)
                result["bb_position"] = max(0.0, min(1.0, bb_pos))
            else:
                result["bb_position"] = 0.5
            result["bb_upper"] = upper
            result["bb_lower"] = lower

    # ATR
    atr = ta.atr(df["high"], df["low"], df["close"], length=14)
    if atr is not None and len(atr) > 0:
        atr_val = float(atr.iloc[-1]) if not pd.isna(atr.iloc[-1]) else 0.0
        result["atr"] = atr_val
        result["atr_pct"] = (atr_val / price * 100) if price > 0 else 0.0

    # Volume Ratio
    vol_lookback = strategy_config.get("volume", {}).get("lookback_periods", 20)
    if len(df) >= vol_lookback and df["volume"].sum() > 0:
        avg_vol = df["volume"].tail(vol_lookback).mean()
        current_vol = df["volume"].iloc[-1]
        result["volume_ratio"] = float(current_vol / avg_vol) if avg_vol > 0 else 1.0
    else:
        result["volume_ratio"] = 1.0

    # Momentum (price change percentages)
    for minutes in strategy_config.get("momentum", {}).get("lookback_periods", [15, 60, 240]):
        bars = min(minutes, len(df) - 1)
        if bars > 0:
            old_price = float(df["close"].iloc[-bars - 1])
            if old_price > 0:
                result[f"pct_{minutes}m"] = ((price - old_price) / old_price) * 100
            else:
                result[f"pct_{minutes}m"] = 0.0
        else:
            result[f"pct_{minutes}m"] = 0.0

    # OBV (On-Balance Volume) trend
    obv = ta.obv(df["close"], df["volume"])
    if obv is not None and len(obv) >= 10:
        obv_sma = obv.tail(10).mean()
        result["obv_trend"] = "RISING" if obv.iloc[-1] > obv_sma else "FALLING"
    else:
        result["obv_trend"] = "FLAT"

    # ADX
    adx_result = ta.adx(df["high"], df["low"], df["close"], length=14)
    if adx_result is not None and len(adx_result) > 0:
        adx_col = [c for c in adx_result.columns if "adx" in c.lower() and "dm" not in c.lower()]
        if adx_col:
            val = adx_result[adx_col[0]].iloc[-1]
            result["adx"] = float(val) if not pd.isna(val) else 20.0

    result["price"] = price
    return result


def compute_signal_score(indicators: dict, strategy_config: dict) -> tuple[float, str]:
    """Compute a 0-100 signal score and direction from indicators.

    Returns (score, direction) where direction is 'LONG' or 'SHORT'.
    """
    weights = strategy_config.get("weights", {"momentum": 30, "trend": 30, "mean_reversion": 20, "volume": 20})

    # Momentum Score (0-30)
    momentum_raw = 0.0
    pct_15m = indicators.get("pct_15m", 0)
    pct_60m = indicators.get("pct_60m", 0)
    pct_240m = indicators.get("pct_240m", 0)

    # Strong moves get higher scores
    avg_momentum = (abs(pct_15m) * 3 + abs(pct_60m) * 2 + abs(pct_240m)) / 6
    momentum_raw = min(avg_momentum / 3.0, 1.0)  # Normalize: 3% avg move = max

    obv_boost = 0.1 if indicators.get("obv_trend") == "RISING" else -0.1
    momentum_raw = max(0, min(1, momentum_raw + obv_boost))
    momentum_score = momentum_raw * weights["momentum"]

    # Determine primary direction from momentum
    short_momentum = pct_15m * 3 + pct_60m * 2 + pct_240m
    direction = "LONG" if short_momentum > 0 else "SHORT"

    # Trend Score (0-30)
    trend_raw = 0.0
    ema_alignment = indicators.get("ema_alignment", "MIXED")
    macd_trend = indicators.get("macd_trend", "FLAT")
    macd_hist = indicators.get("macd_hist", 0)

    if direction == "LONG":
        if ema_alignment == "BULLISH":
            trend_raw += 0.5
        if macd_hist > 0:
            trend_raw += 0.25
        if macd_trend == "RISING":
            trend_raw += 0.25
    else:
        if ema_alignment == "BEARISH":
            trend_raw += 0.5
        if macd_hist < 0:
            trend_raw += 0.25
        if macd_trend == "FALLING":
            trend_raw += 0.25

    trend_score = min(1.0, trend_raw) * weights["trend"]

    # Mean Reversion Score (0-20)
    mr_raw = 0.0
    rsi = indicators.get("rsi", 50)
    bb_pos = indicators.get("bb_position", 0.5)
    rsi_oversold = strategy_config.get("mean_reversion", {}).get("rsi_oversold", 30)
    rsi_overbought = strategy_config.get("mean_reversion", {}).get("rsi_overbought", 70)

    if direction == "LONG":
        if rsi < rsi_oversold:
            mr_raw += 0.5
        elif rsi < 45:
            mr_raw += 0.25
        if bb_pos < 0.2:
            mr_raw += 0.5
        elif bb_pos < 0.4:
            mr_raw += 0.25
    else:
        if rsi > rsi_overbought:
            mr_raw += 0.5
        elif rsi > 55:
            mr_raw += 0.25
        if bb_pos > 0.8:
            mr_raw += 0.5
        elif bb_pos > 0.6:
            mr_raw += 0.25

    mr_score = min(1.0, mr_raw) * weights["mean_reversion"]

    # Volume Score (0-20)
    vol_raw = 0.0
    vol_ratio = indicators.get("volume_ratio", 1.0)
    vol_threshold = strategy_config.get("volume", {}).get("ratio_threshold", 1.5)

    if vol_ratio >= vol_threshold * 2:
        vol_raw = 1.0
    elif vol_ratio >= vol_threshold:
        vol_raw = 0.6
    elif vol_ratio >= 1.0:
        vol_raw = 0.3

    volume_score = vol_raw * weights["volume"]

    total_score = momentum_score + trend_score + mr_score + volume_score
    return round(total_score, 2), direction
