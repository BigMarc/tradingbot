import pytest
import asyncio
import time
from unittest.mock import AsyncMock, MagicMock, patch
from core.signal_engine import SignalEngine, SignalEvent


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


class TestSignalEngine:
    def _make_engine(self):
        exchange = MagicMock()
        exchange.get_tradeable_tokens.return_value = ["BTC", "ETH", "SOL"]

        market_data = MagicMock()
        market_data.get_mid_price.return_value = 3000.0
        market_data.fetch_rest_data = AsyncMock(return_value={
            "ETH": {"volume_24h": 5_000_000, "funding_rate": 0.001, "bid": 2999, "ask": 3001, "last_price": 3000},
            "BTC": {"volume_24h": 50_000_000, "funding_rate": 0.0005, "bid": 64999, "ask": 65001, "last_price": 65000},
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
