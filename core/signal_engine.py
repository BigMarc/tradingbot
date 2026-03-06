from __future__ import annotations

import time
from dataclasses import dataclass, field
from config.settings import load_strategy, load_blacklist
from core.exchange import Exchange
from core.market_data import MarketData
from core.indicators import compute_indicators, compute_signal_score
from storage.database import Database
from utils.logger import logger


@dataclass
class SignalEvent:
    token: str
    direction: str
    score: float
    confidence: float
    indicators: dict
    timestamp: float = field(default_factory=time.time)


class SignalEngine:
    def __init__(self, exchange: Exchange, market_data: MarketData, db: Database) -> None:
        self.exchange = exchange
        self.market_data = market_data
        self.db = db
        self._rest_data: dict[str, dict] = {}
        self._last_rest_fetch: float = 0

    async def scan_for_signals(self) -> list[SignalEvent]:
        strategy = load_strategy()
        blacklist = load_blacklist()
        signal_config = strategy.get("signal", {})
        universe_config = strategy.get("universe", {})
        min_score = signal_config.get("min_score", 65)
        min_volume = universe_config.get("min_24h_volume_usd", 1_000_000)
        max_spread = universe_config.get("max_spread_pct", 0.15)

        # Refresh REST data every 5 minutes
        now = time.time()
        if now - self._last_rest_fetch > 300:
            try:
                self._rest_data = await self.market_data.fetch_rest_data()
                self._last_rest_fetch = now
            except Exception as e:
                logger.warning("Failed to fetch REST data: {}", e)

        # Get open positions to avoid duplicate entries
        open_trades = await self.db.get_open_trades()
        open_tokens = {t["token"] for t in open_trades}
        max_positions = strategy.get("trading", {}).get("max_positions", 2)

        if len(open_trades) >= max_positions:
            return []

        signals: list[SignalEvent] = []
        tokens = self.exchange.get_tradeable_tokens()

        for token in tokens:
            # Blacklist check
            if token in blacklist:
                continue

            # Already has position
            if token in open_tokens:
                continue

            # Volume check
            rest_info = self._rest_data.get(token, {})
            volume_24h = rest_info.get("volume_24h", 0)
            if volume_24h < min_volume:
                continue

            # Spread check
            bid = rest_info.get("bid", 0)
            ask = rest_info.get("ask", 0)
            if bid > 0 and ask > 0:
                spread_pct = ((ask - bid) / bid) * 100
                if spread_pct > max_spread:
                    continue
            else:
                # If we don't have bid/ask data, use mid price
                mid = self.market_data.get_mid_price(token)
                if not mid:
                    continue

            # Funding rate check
            funding_rate = rest_info.get("funding_rate", 0)

            # Get candles and compute indicators
            candles = await self.db.get_candles(token, "1m", limit=300)
            if len(candles) < 50:
                continue

            indicators = compute_indicators(candles, signal_config)
            if not indicators:
                continue

            # Add rest data to indicators
            indicators["volume_24h"] = volume_24h
            indicators["funding_rate"] = funding_rate
            indicators["spread_pct"] = spread_pct if bid > 0 else 0

            score, direction = compute_signal_score(indicators, signal_config)

            # Funding rate penalty
            if direction == "LONG" and funding_rate > 0.01:
                score *= 0.8
            elif direction == "SHORT" and funding_rate < -0.01:
                score *= 0.8

            if score >= min_score:
                confidence = min(score / 100.0, 0.95)
                signal = SignalEvent(
                    token=token,
                    direction=direction,
                    score=score,
                    confidence=confidence,
                    indicators=indicators,
                )
                signals.append(signal)
                logger.info(
                    "Signal: {} {} | Score: {:.1f} | RSI: {:.1f} | Vol Ratio: {:.1f}x",
                    direction, token, score, indicators.get("rsi", 0), indicators.get("volume_ratio", 0),
                )

                # Store signal in DB
                await self.db.insert_signal(token, direction, score, indicators)

        # Sort by score descending
        signals.sort(key=lambda s: s.score, reverse=True)
        return signals

    def get_btc_trend(self) -> str:
        """Get BTC 1h trend based on mid prices."""
        # Simple heuristic from mid prices
        mid = self.market_data.get_mid_price("BTC")
        if not mid:
            return "UNKNOWN"
        return "NEUTRAL"

    def get_market_sentiment(self) -> str:
        """Simple market sentiment from available tokens."""
        prices = self.market_data.mid_prices
        if len(prices) < 5:
            return "UNKNOWN"
        return "NEUTRAL"

    def get_top_movers(self) -> list[dict]:
        """Get top 3 movers by momentum."""
        return []
