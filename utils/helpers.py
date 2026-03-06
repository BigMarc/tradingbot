from __future__ import annotations

import math
import time
from datetime import datetime, timezone


def round_price(price: float, tick_size: float) -> float:
    if tick_size <= 0:
        return price
    precision = max(0, -int(math.floor(math.log10(tick_size))))
    return round(round(price / tick_size) * tick_size, precision)


def round_size(size: float, step_size: float) -> float:
    if step_size <= 0:
        return size
    precision = max(0, -int(math.floor(math.log10(step_size))))
    return round(math.floor(size / step_size) * step_size, precision)


def pct_change(old: float, new: float) -> float:
    if old == 0:
        return 0.0
    return ((new - old) / old) * 100.0


def format_usd(value: float) -> str:
    if abs(value) >= 1_000_000:
        return f"${value / 1_000_000:.2f}M"
    if abs(value) >= 1_000:
        return f"${value / 1_000:.2f}K"
    return f"${value:.2f}"


def format_pct(value: float) -> str:
    sign = "+" if value > 0 else ""
    return f"{sign}{value:.2f}%"


def ts_to_str(ts: float) -> str:
    return datetime.fromtimestamp(ts, tz=timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC")


def minutes_ago(ts: float) -> float:
    return (time.time() - ts) / 60.0


def candle_start(ts: float, interval_seconds: int) -> float:
    return ts - (ts % interval_seconds)
