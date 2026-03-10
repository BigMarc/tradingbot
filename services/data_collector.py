from __future__ import annotations

import asyncio
import time
from core.exchange import Exchange
from core.market_data import MarketData
from storage.database import Database
from utils.logger import logger


class DataCollector:
    """Manages WebSocket data feed and periodic REST data fetches."""

    def __init__(self, exchange: Exchange, market_data: MarketData, db: Database) -> None:
        self.exchange = exchange
        self.market_data = market_data
        self.db = db
        self._running = False
        self._health_monitor = None

    def set_health_monitor(self, health_monitor) -> None:
        self._health_monitor = health_monitor

    def _heartbeat(self) -> None:
        if self._health_monitor:
            self._health_monitor.heartbeat("data_collector")

    async def start(self) -> None:
        self._running = True
        logger.info("DataCollector starting...")

        # Initial token list
        tokens = self.exchange.get_tradeable_tokens()
        self.market_data.update_subscribed_tokens(tokens[:20])
        logger.info("Subscribing to {} tokens", min(len(tokens), 20))

        # Pre-load historical candles so signals work immediately
        await self._preload_historical_candles(tokens[:20])

        # Run tasks concurrently
        await asyncio.gather(
            self._run_websocket(),
            self._run_rest_fetcher(),
            self._run_token_updater(),
            self._run_cleanup(),
            self._run_heartbeat(),
        )

    async def _preload_historical_candles(self, tokens: list[str]) -> None:
        """Fetch historical 5m candles from exchange REST API so indicators work immediately."""
        logger.info("Pre-loading historical 5m candles for {} tokens...", len(tokens))
        loaded = 0
        for token in tokens:
            symbol = f"{token}/USDC:USDC"
            try:
                ohlcv = await self.exchange.fetch_ohlcv(symbol, timeframe="5m", limit=300)
                for candle in ohlcv:
                    ts, o, h, l, c, v = candle[0] / 1000, candle[1], candle[2], candle[3], candle[4], candle[5]
                    await self.db.upsert_candle(token, "5m", ts, o, h, l, c, v)
                loaded += 1
                logger.debug("Loaded {} 5m candles for {}", len(ohlcv), token)
            except Exception as e:
                logger.warning("Failed to preload candles for {}: {}", token, e)
            # Small delay to avoid rate limits
            await asyncio.sleep(0.3)
        logger.info("Historical candle preload complete: {}/{} tokens loaded", loaded, len(tokens))

    async def stop(self) -> None:
        self._running = False
        await self.market_data.stop()

    async def _run_websocket(self) -> None:
        """WebSocket price feed - runs continuously."""
        while self._running:
            # Pause during exchange maintenance
            if self._health_monitor and self._health_monitor.is_maintenance_mode:
                logger.debug("Maintenance mode active, pausing WebSocket reconnect")
                await asyncio.sleep(30)
                continue
            try:
                await self.market_data.start()
            except Exception as e:
                logger.error("WebSocket feed error: {}", e)
                if self._health_monitor:
                    self._health_monitor.record_ws_disconnect()
                if self._running:
                    await asyncio.sleep(5)

    async def _run_rest_fetcher(self) -> None:
        """Fetch supplementary REST data every 5 minutes."""
        while self._running:
            try:
                await asyncio.sleep(300)  # 5 minutes
                if not self._running:
                    break
                # Skip during maintenance
                if self._health_monitor and self._health_monitor.is_maintenance_mode:
                    logger.debug("Maintenance mode, skipping REST fetch")
                    continue
                data = await self.market_data.fetch_rest_data()
                logger.debug("REST data fetched for {} tokens", len(data))
            except asyncio.CancelledError:
                break
            except Exception as e:
                logger.error("REST fetcher error: {}", e)
                await asyncio.sleep(60)

    async def _run_token_updater(self) -> None:
        """Update subscribed token list every 15 minutes."""
        while self._running:
            try:
                await asyncio.sleep(900)  # 15 minutes
                if not self._running:
                    break
                tokens = self.exchange.get_tradeable_tokens()
                self.market_data.update_subscribed_tokens(tokens[:20])
                logger.info("Updated token subscriptions: {} tokens", min(len(tokens), 20))
            except asyncio.CancelledError:
                break
            except Exception as e:
                logger.error("Token updater error: {}", e)

    async def _run_heartbeat(self) -> None:
        """Send periodic heartbeat so health monitor knows we're alive."""
        while self._running:
            try:
                self._heartbeat()
                await asyncio.sleep(60)
            except asyncio.CancelledError:
                break

    async def _run_cleanup(self) -> None:
        """Clean up old data periodically (every 6 hours)."""
        while self._running:
            try:
                await asyncio.sleep(21600)  # 6 hours
                if not self._running:
                    break
                await self.db.cleanup_old_data()
                logger.info("Data cleanup completed")
            except asyncio.CancelledError:
                break
            except Exception as e:
                logger.error("Cleanup error: {}", e)
