from __future__ import annotations

import asyncio
import time
import orjson
import websockets
from websockets.exceptions import ConnectionClosed
from core.exchange import Exchange
from storage.database import Database
from utils.logger import logger
from utils.helpers import candle_start


class MarketData:
    def __init__(self, exchange: Exchange, db: Database) -> None:
        self.exchange = exchange
        self.db = db
        self._ws: websockets.WebSocketClientProtocol | None = None
        self._mid_prices: dict[str, float] = {}
        self._subscribed_tokens: list[str] = []
        self._running = False
        self._candle_buffers: dict[str, dict] = {}  # token -> {open, high, low, close, volume, ts}
        self._last_candle_flush: float = 0
        self._reconnect_delays = [5, 15, 30, 60]

    @property
    def mid_prices(self) -> dict[str, float]:
        return self._mid_prices.copy()

    def get_mid_price(self, token: str) -> float | None:
        return self._mid_prices.get(token)

    async def start(self) -> None:
        self._running = True
        while self._running:
            try:
                await self._connect_and_listen()
            except (ConnectionClosed, OSError, asyncio.TimeoutError) as e:
                if not self._running:
                    break
                for delay in self._reconnect_delays:
                    logger.warning("WebSocket disconnected ({}), reconnecting in {}s...", type(e).__name__, delay)
                    await asyncio.sleep(delay)
                    try:
                        await self._connect_and_listen()
                        break
                    except Exception:
                        continue
                else:
                    logger.error("All reconnect attempts failed, retrying from start...")
                    await asyncio.sleep(5)

    async def stop(self) -> None:
        self._running = False
        if self._ws:
            await self._ws.close()
            self._ws = None

    async def _connect_and_listen(self) -> None:
        url = self.exchange.get_ws_url()
        logger.info("Connecting to WebSocket: {}", url)

        async with websockets.connect(url, ping_interval=30, ping_timeout=10) as ws:
            self._ws = ws
            logger.info("WebSocket connected")

            # Subscribe to allMids
            await ws.send(orjson.dumps({
                "method": "subscribe",
                "subscription": {"type": "allMids"}
            }).decode())

            # Subscribe to trades for top tokens
            for token in self._subscribed_tokens[:20]:
                await ws.send(orjson.dumps({
                    "method": "subscribe",
                    "subscription": {"type": "trades", "coin": token}
                }).decode())

            logger.info("Subscribed to allMids and trades for {} tokens", min(len(self._subscribed_tokens), 20))

            async for raw_msg in ws:
                if not self._running:
                    break
                try:
                    msg = orjson.loads(raw_msg)
                    await self._handle_message(msg)
                except Exception as e:
                    logger.debug("Error handling WS message: {}", e)

    async def _handle_message(self, msg: dict) -> None:
        channel = msg.get("channel")
        data = msg.get("data")
        if not channel or not data:
            return

        if channel == "allMids":
            await self._handle_all_mids(data)
        elif channel == "trades":
            await self._handle_trades(data)

    async def _handle_all_mids(self, data: dict) -> None:
        mids = data.get("mids", {})
        now = time.time()
        ticks = []
        subscribed = set(self._subscribed_tokens)
        for token, mid_str in mids.items():
            mid = float(mid_str)
            self._mid_prices[token] = mid  # Keep all mid prices in memory (free)
            if token in subscribed:  # Only persist subscribed tokens to DB
                ticks.append((now, token, None, None, mid, 0))
                self._update_candle_buffer(token, mid, now)

        if ticks:
            await self.db.insert_ticks_batch(ticks)

        # Flush candles every 60 seconds
        if now - self._last_candle_flush >= 60:
            await self._flush_candles()
            self._last_candle_flush = now

    async def _handle_trades(self, data: list) -> None:
        for trade in data:
            token = trade.get("coin", "")
            price = float(trade.get("px", 0))
            size = float(trade.get("sz", 0))
            if token and price > 0:
                self._mid_prices[token] = price
                self._update_candle_buffer(token, price, time.time(), size)

    def _update_candle_buffer(self, token: str, price: float, ts: float, volume: float = 0) -> None:
        candle_ts = candle_start(ts, 60)  # 1-minute candles
        key = f"{token}:{candle_ts}"

        if key not in self._candle_buffers:
            self._candle_buffers[key] = {
                "token": token,
                "ts": candle_ts,
                "open": price,
                "high": price,
                "low": price,
                "close": price,
                "volume": volume,
            }
        else:
            buf = self._candle_buffers[key]
            buf["high"] = max(buf["high"], price)
            buf["low"] = min(buf["low"], price)
            buf["close"] = price
            buf["volume"] += volume

    async def _flush_candles(self) -> None:
        now = time.time()
        current_candle_ts = candle_start(now, 60)
        flushed = 0

        keys_to_remove = []
        for key, buf in self._candle_buffers.items():
            # Only flush completed candles (not the current one)
            if buf["ts"] < current_candle_ts:
                await self.db.upsert_candle(
                    buf["token"], "1m", buf["ts"],
                    buf["open"], buf["high"], buf["low"], buf["close"], buf["volume"]
                )
                keys_to_remove.append(key)
                flushed += 1

                # Also aggregate higher timeframes
                await self._aggregate_candle(buf["token"], buf["ts"])

        for key in keys_to_remove:
            del self._candle_buffers[key]

        if flushed > 0:
            logger.debug("Flushed {} 1m candles", flushed)

    async def _aggregate_candle(self, token: str, ts_1m: float) -> None:
        for tf, seconds in [("5m", 300), ("15m", 900), ("1h", 3600)]:
            tf_start = candle_start(ts_1m, seconds)
            candles = await self.db.get_candles(token, "1m", limit=seconds // 60)
            relevant = [c for c in candles if candle_start(c["timestamp"], seconds) == tf_start]
            if relevant:
                o = relevant[0]["open"]
                h = max(c["high"] for c in relevant)
                l = min(c["low"] for c in relevant)
                c_val = relevant[-1]["close"]
                v = sum(c["volume"] for c in relevant)
                await self.db.upsert_candle(token, tf, tf_start, o, h, l, c_val, v)

    def update_subscribed_tokens(self, tokens: list[str]) -> None:
        self._subscribed_tokens = tokens[:20]

    async def fetch_rest_data(self) -> dict[str, dict]:
        """Fetch supplementary data via REST (funding rates, etc.)."""
        result = {}
        funding_batch = []
        now = time.time()

        for token in self._subscribed_tokens[:20]:
            symbol = f"{token}/USDC:USDC"
            try:
                funding = await self.exchange.fetch_funding_rate(symbol)
                ticker = await self.exchange.fetch_ticker(symbol)
                funding_rate = funding.get("fundingRate", 0)
                if funding_rate is None:
                    funding_rate = 0

                # Hyperliquid funds every 8h (3x per day)
                annualized_pct = funding_rate * 3 * 365 * 100

                result[token] = {
                    "funding_rate": funding_rate,
                    "funding_annualized_pct": annualized_pct,
                    "volume_24h": ticker.get("quoteVolume", 0),
                    "last_price": ticker.get("last", 0),
                    "bid": ticker.get("bid", 0),
                    "ask": ticker.get("ask", 0),
                }

                funding_batch.append((now, token, funding_rate, annualized_pct))
            except Exception as e:
                logger.debug("Failed to fetch REST data for {}: {}", token, e)

        # Store funding rates in DB
        if funding_batch:
            try:
                await self.db.insert_funding_rates_batch(funding_batch)
                logger.debug("Stored funding rates for {} tokens", len(funding_batch))
            except Exception as e:
                logger.warning("Failed to store funding rates: {}", e)

        return result
