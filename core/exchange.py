from __future__ import annotations

import ccxt
import asyncio
from tenacity import retry, stop_after_attempt, wait_exponential, retry_if_exception_type
from config.settings import settings, load_strategy
from utils.logger import logger


class Exchange:
    def __init__(self) -> None:
        self._exchange: ccxt.hyperliquid | None = None
        self._markets: dict = {}

    def _create_exchange(self) -> ccxt.hyperliquid:
        ex = ccxt.hyperliquid({
            "walletAddress": settings.hyperliquid_wallet_address,
            "privateKey": settings.hyperliquid_api_private_key,
            "options": {"defaultType": "swap"},
            "enableRateLimit": True,
        })
        if settings.network == "testnet":
            ex.set_sandbox_mode(True)
            logger.info("Exchange set to TESTNET mode")
        else:
            logger.info("Exchange set to MAINNET mode")
        return ex

    async def connect(self) -> None:
        self._exchange = self._create_exchange()
        self._markets = await asyncio.to_thread(self._exchange.load_markets)
        logger.info("Loaded {} markets from Hyperliquid", len(self._markets))

    @property
    def exchange(self) -> ccxt.hyperliquid:
        assert self._exchange is not None, "Exchange not connected"
        return self._exchange

    @property
    def markets(self) -> dict:
        return self._markets

    def get_ws_url(self) -> str:
        if settings.network == "testnet":
            return "wss://api.hyperliquid-testnet.xyz/ws"
        return "wss://api.hyperliquid.xyz/ws"

    def get_rest_url(self) -> str:
        if settings.network == "testnet":
            return "https://api.hyperliquid-testnet.xyz"
        return "https://api.hyperliquid.xyz"

    def get_tradeable_tokens(self) -> list[str]:
        tokens = []
        for symbol, market in self._markets.items():
            if market.get("swap") and market.get("active"):
                base = market.get("base", "")
                if base:
                    tokens.append(base)
        return sorted(set(tokens))

    def get_market_info(self, token: str) -> dict | None:
        symbol = f"{token}/USDC:USDC"
        return self._markets.get(symbol)

    def get_tick_size(self, token: str) -> float:
        info = self.get_market_info(token)
        if info and info.get("precision", {}).get("price"):
            return 10 ** (-info["precision"]["price"])
        return 0.01

    def get_step_size(self, token: str) -> float:
        info = self.get_market_info(token)
        if info and info.get("precision", {}).get("amount"):
            return 10 ** (-info["precision"]["amount"])
        return 0.001

    @retry(stop=stop_after_attempt(3), wait=wait_exponential(multiplier=1, min=2, max=10), retry=retry_if_exception_type(ccxt.NetworkError))
    async def fetch_balance(self) -> dict:
        return await asyncio.to_thread(self.exchange.fetch_balance)

    @retry(stop=stop_after_attempt(3), wait=wait_exponential(multiplier=1, min=2, max=10), retry=retry_if_exception_type(ccxt.NetworkError))
    async def fetch_ticker(self, symbol: str) -> dict:
        return await asyncio.to_thread(self.exchange.fetch_ticker, symbol)

    @retry(stop=stop_after_attempt(3), wait=wait_exponential(multiplier=1, min=2, max=10), retry=retry_if_exception_type(ccxt.NetworkError))
    async def fetch_order_book(self, symbol: str, limit: int = 5) -> dict:
        return await asyncio.to_thread(self.exchange.fetch_order_book, symbol, limit)

    @retry(stop=stop_after_attempt(3), wait=wait_exponential(multiplier=1, min=2, max=10), retry=retry_if_exception_type(ccxt.NetworkError))
    async def fetch_ohlcv(self, symbol: str, timeframe: str = "1m", limit: int = 100) -> list:
        return await asyncio.to_thread(self.exchange.fetch_ohlcv, symbol, timeframe, None, limit)

    @retry(stop=stop_after_attempt(3), wait=wait_exponential(multiplier=1, min=2, max=10), retry=retry_if_exception_type(ccxt.NetworkError))
    async def fetch_funding_rate(self, symbol: str) -> dict:
        return await asyncio.to_thread(self.exchange.fetch_funding_rate, symbol)

    @retry(stop=stop_after_attempt(3), wait=wait_exponential(multiplier=1, min=2, max=10), retry=retry_if_exception_type(ccxt.NetworkError))
    async def fetch_positions(self) -> list:
        return await asyncio.to_thread(self.exchange.fetch_positions)

    @retry(stop=stop_after_attempt(3), wait=wait_exponential(multiplier=1, min=2, max=10), retry=retry_if_exception_type(ccxt.NetworkError))
    async def create_order(self, symbol: str, order_type: str, side: str, amount: float, price: float | None = None, params: dict | None = None) -> dict:
        return await asyncio.to_thread(
            self.exchange.create_order, symbol, order_type, side, amount, price, params or {}
        )

    @retry(stop=stop_after_attempt(3), wait=wait_exponential(multiplier=1, min=2, max=10), retry=retry_if_exception_type(ccxt.NetworkError))
    async def cancel_order(self, order_id: str, symbol: str) -> dict:
        return await asyncio.to_thread(self.exchange.cancel_order, order_id, symbol)

    @retry(stop=stop_after_attempt(3), wait=wait_exponential(multiplier=1, min=2, max=10), retry=retry_if_exception_type(ccxt.NetworkError))
    async def cancel_all_orders(self, symbol: str | None = None) -> list:
        return await asyncio.to_thread(self.exchange.cancel_all_orders, symbol)

    @retry(stop=stop_after_attempt(3), wait=wait_exponential(multiplier=1, min=2, max=10), retry=retry_if_exception_type(ccxt.NetworkError))
    async def set_leverage(self, leverage: int, symbol: str) -> None:
        await asyncio.to_thread(self.exchange.set_leverage, leverage, symbol)
