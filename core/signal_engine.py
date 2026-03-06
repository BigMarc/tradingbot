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
    funding_modifier: float = 0.0
    timestamp: float = field(default_factory=time.time)


def get_funding_label(annualized_pct: float) -> str:
    """Classify funding rate into a human-readable label."""
    abs_pct = abs(annualized_pct)
    if abs_pct < 10:
        return "NEUTRAL"
    direction = "LONG_HEAVY" if annualized_pct > 0 else "SHORT_HEAVY"
    if abs_pct < 30:
        return f"LEICHT_{direction}"
    if abs_pct < 80:
        return f"STARK_{direction}"
    return f"EXTREM_{direction}"


def apply_funding_modifier(
    base_score: float,
    signal_direction: str,
    funding_rate: float,
    funding_config: dict,
) -> tuple[float, float, bool]:
    """Apply funding rate modifier to signal score.

    Returns (modified_score, modifier_applied, is_blocked).
    """
    if not funding_config.get("enabled", True):
        return base_score, 0.0, False

    # Annualize: Hyperliquid funds every 8h (3x/day)
    annual_pct = funding_rate * 3 * 365 * 100

    moderate_threshold = funding_config.get("moderate_threshold", 15.0)
    extreme_threshold = funding_config.get("extreme_threshold", 50.0)
    block_threshold = funding_config.get("extreme_block_threshold", 100.0)
    contrarian_bonus = funding_config.get("contrarian_bonus", 8)
    contrarian_bonus_extreme = funding_config.get("contrarian_bonus_extreme", 15)
    crowded_penalty = funding_config.get("crowded_penalty", 10)
    crowded_penalty_extreme = funding_config.get("crowded_penalty_extreme", 20)

    # Check for hard block
    if signal_direction == "LONG" and annual_pct > block_threshold:
        return 0.0, -base_score, True
    if signal_direction == "SHORT" and annual_pct < -block_threshold:
        return 0.0, -base_score, True

    # Normal funding - no modifier
    if abs(annual_pct) < moderate_threshold:
        return base_score, 0.0, False

    is_extreme = abs(annual_pct) >= extreme_threshold
    modifier = 0.0

    if signal_direction == "LONG":
        if funding_rate < 0:
            # Negative funding + Long = Contrarian (good)
            modifier = contrarian_bonus_extreme if is_extreme else contrarian_bonus
        else:
            # Positive funding + Long = Crowded (bad)
            modifier = -(crowded_penalty_extreme if is_extreme else crowded_penalty)
    elif signal_direction == "SHORT":
        if funding_rate > 0:
            # Positive funding + Short = Contrarian (good)
            modifier = contrarian_bonus_extreme if is_extreme else contrarian_bonus
        else:
            # Negative funding + Short = Crowded (bad)
            modifier = -(crowded_penalty_extreme if is_extreme else crowded_penalty)

    modified_score = max(0, min(100, base_score + modifier))
    return modified_score, modifier, False


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
        funding_config = signal_config.get("funding_rate", {})
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
            spread_pct = 0.0
            if bid > 0 and ask > 0:
                spread_pct = ((ask - bid) / bid) * 100
                if spread_pct > max_spread:
                    continue
            else:
                mid = self.market_data.get_mid_price(token)
                if not mid:
                    continue

            # Funding rate data
            funding_rate = rest_info.get("funding_rate", 0) or 0
            funding_annualized = rest_info.get("funding_annualized_pct", 0)

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
            indicators["funding_annualized_pct"] = funding_annualized
            indicators["funding_label"] = get_funding_label(funding_annualized)
            indicators["spread_pct"] = spread_pct

            # Compute base score
            raw_score, direction = compute_signal_score(indicators, signal_config)

            # Apply funding rate modifier (Extension 3)
            final_score, modifier, is_blocked = apply_funding_modifier(
                raw_score, direction, funding_rate, funding_config,
            )

            if is_blocked:
                logger.info(
                    "Signal BLOCKED by funding rate: {} {} | Funding: {:.4f} ({:.1f}% annual)",
                    direction, token, funding_rate, funding_annualized,
                )
                continue

            indicators["funding_modifier"] = modifier

            if final_score >= min_score:
                confidence = min(final_score / 100.0, 0.95)
                signal = SignalEvent(
                    token=token,
                    direction=direction,
                    score=final_score,
                    confidence=confidence,
                    indicators=indicators,
                    funding_modifier=modifier,
                )
                signals.append(signal)
                logger.info(
                    "Signal: {} {} | Score: {:.1f} (funding mod: {:+.0f}) | RSI: {:.1f} | Vol: {:.1f}x | Funding: {}",
                    direction, token, final_score, modifier,
                    indicators.get("rsi", 0), indicators.get("volume_ratio", 0),
                    indicators.get("funding_label", "N/A"),
                )

                # Store signal in DB
                await self.db.insert_signal(token, direction, final_score, indicators)

        # Sort by score descending
        signals.sort(key=lambda s: s.score, reverse=True)
        return signals

    def get_btc_trend(self) -> str:
        """Get BTC 1h trend based on mid prices."""
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
