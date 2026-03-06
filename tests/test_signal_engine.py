import pytest
import asyncio
import time
from unittest.mock import AsyncMock, MagicMock, patch
from core.signal_engine import (
    SignalEngine, SignalEvent,
    apply_funding_modifier, get_funding_label,
)


class TestSignalEvent:
    def test_signal_event_creation(self):
        event = SignalEvent(
            token="ETH",
            direction="LONG",
            score=75.0,
            confidence=0.75,
            indicators={"rsi": 35, "price": 3000},
        )
        assert event.token == "ETH"
        assert event.direction == "LONG"
        assert event.score == 75.0
        assert event.confidence == 0.75
        assert event.timestamp > 0

    def test_signal_event_with_funding_modifier(self):
        event = SignalEvent(
            token="ETH",
            direction="LONG",
            score=75.0,
            confidence=0.75,
            indicators={},
            funding_modifier=8.0,
        )
        assert event.funding_modifier == 8.0


class TestFundingLabel:
    def test_neutral(self):
        assert get_funding_label(5.0) == "NEUTRAL"
        assert get_funding_label(-5.0) == "NEUTRAL"
        assert get_funding_label(0.0) == "NEUTRAL"

    def test_slight_long_heavy(self):
        assert get_funding_label(15.0) == "LEICHT_LONG_HEAVY"

    def test_slight_short_heavy(self):
        assert get_funding_label(-15.0) == "LEICHT_SHORT_HEAVY"

    def test_strong_long_heavy(self):
        assert get_funding_label(50.0) == "STARK_LONG_HEAVY"

    def test_extreme_short_heavy(self):
        assert get_funding_label(-90.0) == "EXTREM_SHORT_HEAVY"


class TestFundingModifier:
    DEFAULT_CONFIG = {
        "enabled": True,
        "moderate_threshold": 15.0,
        "extreme_threshold": 50.0,
        "extreme_block_threshold": 100.0,
        "contrarian_bonus": 8,
        "contrarian_bonus_extreme": 15,
        "crowded_penalty": 10,
        "crowded_penalty_extreme": 20,
    }

    def test_no_effect_normal_funding(self):
        # 0.001 rate = ~1.1% annual < 15% threshold
        score, mod, blocked = apply_funding_modifier(70, "LONG", 0.001, self.DEFAULT_CONFIG)
        assert score == 70
        assert mod == 0.0
        assert not blocked

    def test_disabled(self):
        config = {**self.DEFAULT_CONFIG, "enabled": False}
        score, mod, blocked = apply_funding_modifier(70, "LONG", 0.1, config)
        assert score == 70
        assert not blocked

    def test_contrarian_long_with_negative_funding(self):
        # Negative funding + LONG = contrarian (good)
        # rate = -0.02 -> annual = -0.02 * 3 * 365 * 100 = -2190% (extreme)
        score, mod, blocked = apply_funding_modifier(70, "LONG", -0.02, self.DEFAULT_CONFIG)
        assert mod == 15  # extreme contrarian bonus
        assert score == 85
        assert not blocked

    def test_crowded_long_with_positive_funding(self):
        # Positive funding + LONG = crowded (bad)
        # rate = 0.02 -> annual = 2190% (extreme, but check block first)
        # Block threshold is 100%, so 2190% > 100% -> blocked
        score, mod, blocked = apply_funding_modifier(70, "LONG", 0.02, self.DEFAULT_CONFIG)
        assert blocked

    def test_moderate_crowded_penalty(self):
        # rate = 0.0002 -> annual = ~21.9% (moderate, above 15%, below 50%)
        score, mod, blocked = apply_funding_modifier(70, "LONG", 0.0002, self.DEFAULT_CONFIG)
        assert mod == -10  # moderate crowded penalty
        assert score == 60
        assert not blocked

    def test_contrarian_short_with_positive_funding(self):
        # Positive funding + SHORT = contrarian (good)
        # rate = 0.0003 -> annual ~32.9% (moderate)
        score, mod, blocked = apply_funding_modifier(70, "SHORT", 0.0003, self.DEFAULT_CONFIG)
        assert mod == 8  # moderate contrarian bonus
        assert score == 78
        assert not blocked

    def test_blocked_short_with_extreme_negative(self):
        # rate = -0.01 -> annual = -1095% > 100% threshold -> block
        score, mod, blocked = apply_funding_modifier(70, "SHORT", -0.01, self.DEFAULT_CONFIG)
        assert blocked

    def test_score_capped_at_100(self):
        score, mod, blocked = apply_funding_modifier(95, "LONG", -0.001, self.DEFAULT_CONFIG)
        assert score <= 100

    def test_score_floored_at_0(self):
        score, mod, blocked = apply_funding_modifier(5, "LONG", 0.0003, self.DEFAULT_CONFIG)
        assert score >= 0

    def test_zero_funding_rate(self):
        score, mod, blocked = apply_funding_modifier(70, "LONG", 0.0, self.DEFAULT_CONFIG)
        assert score == 70
        assert not blocked


class TestSignalEngine:
    def _make_engine(self):
        exchange = MagicMock()
        exchange.get_tradeable_tokens.return_value = ["BTC", "ETH", "SOL"]

        market_data = MagicMock()
        market_data.get_mid_price.return_value = 3000.0
        market_data.fetch_rest_data = AsyncMock(return_value={
            "ETH": {"volume_24h": 5_000_000, "funding_rate": 0.001, "funding_annualized_pct": 109.5, "bid": 2999, "ask": 3001, "last_price": 3000},
            "BTC": {"volume_24h": 50_000_000, "funding_rate": 0.0005, "funding_annualized_pct": 54.75, "bid": 64999, "ask": 65001, "last_price": 65000},
        })

        db = MagicMock()
        db.get_open_trades = AsyncMock(return_value=[])
        db.get_candles = AsyncMock(return_value=[])
        db.insert_signal = AsyncMock(return_value=1)

        return SignalEngine(exchange, market_data, db)

    @pytest.mark.asyncio
    async def test_no_signals_without_candles(self):
        engine = self._make_engine()
        signals = await engine.scan_for_signals()
        assert signals == []

    @pytest.mark.asyncio
    async def test_no_signals_when_max_positions(self):
        engine = self._make_engine()
        engine.db.get_open_trades = AsyncMock(return_value=[
            {"id": 1, "token": "BTC"},
            {"id": 2, "token": "SOL"},
        ])
        signals = await engine.scan_for_signals()
        assert signals == []

    def test_btc_trend_without_data(self):
        engine = self._make_engine()
        engine.market_data.get_mid_price.return_value = None
        assert engine.get_btc_trend() == "UNKNOWN"

    def test_market_sentiment_without_data(self):
        engine = self._make_engine()
        engine.market_data.mid_prices = {}
        assert engine.get_market_sentiment() in ("UNKNOWN", "NEUTRAL")
