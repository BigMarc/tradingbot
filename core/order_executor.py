from __future__ import annotations

import asyncio
from core.exchange import Exchange
from storage.database import Database
from utils.logger import logger
from utils.helpers import round_price, round_size


class OrderExecutor:
    def __init__(self, exchange: Exchange, db: Database) -> None:
        self.exchange = exchange
        self.db = db

    async def open_position(
        self,
        token: str,
        direction: str,
        leverage: int,
        size_usd: float,
        entry_type: str = "MARKET",
        limit_price: float | None = None,
        ai_reasoning: str = "",
    ) -> dict | None:
        """Open a new position. Returns trade info dict or None on failure."""
        symbol = f"{token}/USDC:USDC"

        try:
            # Set leverage
            await self.exchange.set_leverage(leverage, symbol)
        except Exception as e:
            logger.warning("Failed to set leverage for {}: {}", symbol, e)

        # Get current price for sizing
        try:
            ticker = await self.exchange.fetch_ticker(symbol)
            current_price = ticker.get("last", 0)
            if current_price <= 0:
                logger.error("Invalid price for {}: {}", symbol, current_price)
                return None
        except Exception as e:
            logger.error("Failed to fetch ticker for {}: {}", symbol, e)
            return None

        # Calculate amount
        amount = size_usd / current_price
        step_size = self.exchange.get_step_size(token)
        amount = round_size(amount, step_size)

        if amount <= 0:
            logger.error("Amount too small for {}: size_usd={}", token, size_usd)
            return None

        side = "buy" if direction == "LONG" else "sell"

        try:
            if entry_type == "LIMIT" and limit_price:
                tick_size = self.exchange.get_tick_size(token)
                limit_price = round_price(limit_price, tick_size)
                order = await self.exchange.create_order(symbol, "limit", side, amount, limit_price)
            else:
                order = await self.exchange.create_order(symbol, "market", side, amount)
        except Exception as e:
            logger.error("Failed to place {} order for {}: {}", entry_type, symbol, e)
            return None

        fill_price = order.get("average", order.get("price", current_price))
        fees = order.get("fee", {}).get("cost", 0) or 0
        order_id = order.get("id", "")

        logger.info(
            "OPENED {} {} | Price: {} | Amount: {} | Size: ${:.2f} | Leverage: {}x | Fees: ${:.4f}",
            direction, token, fill_price, amount, size_usd, leverage, fees,
        )

        # Record in database
        trade_id = await self.db.insert_trade(
            token=token,
            direction=direction,
            entry_price=fill_price or current_price,
            leverage=leverage,
            size_usd=size_usd,
            ai_reasoning=ai_reasoning,
        )

        return {
            "trade_id": trade_id,
            "order_id": order_id,
            "token": token,
            "direction": direction,
            "entry_price": fill_price or current_price,
            "amount": amount,
            "size_usd": size_usd,
            "leverage": leverage,
            "fees": fees,
        }

    async def close_position(
        self,
        token: str,
        direction: str,
        trade_id: int,
        entry_price: float,
        leverage: int,
        size_usd: float,
        close_pct: float = 100.0,
        exit_reason: str = "manual",
    ) -> dict | None:
        """Close a position (full or partial). Returns result dict."""
        symbol = f"{token}/USDC:USDC"

        try:
            ticker = await self.exchange.fetch_ticker(symbol)
            current_price = ticker.get("last", 0)
        except Exception as e:
            logger.error("Failed to fetch price for close {}: {}", token, e)
            return None

        # Calculate close amount
        total_amount = size_usd / entry_price
        close_amount = total_amount * (close_pct / 100.0)
        step_size = self.exchange.get_step_size(token)
        close_amount = round_size(close_amount, step_size)

        if close_amount <= 0:
            logger.warning("Close amount too small for {}", token)
            return None

        # Close: opposite side
        side = "sell" if direction == "LONG" else "buy"

        try:
            order = await self.exchange.create_order(symbol, "market", side, close_amount, params={"reduceOnly": True})
        except Exception as e:
            logger.error("Failed to close position {}: {}", token, e)
            return None

        fill_price = order.get("average", order.get("price", current_price))
        fees = order.get("fee", {}).get("cost", 0) or 0

        # Calculate PnL
        if direction == "LONG":
            pnl_pct = ((fill_price - entry_price) / entry_price) * 100 * leverage
        else:
            pnl_pct = ((entry_price - fill_price) / entry_price) * 100 * leverage

        closed_size = size_usd * (close_pct / 100.0)
        pnl_usd = closed_size * (pnl_pct / 100.0)

        logger.info(
            "CLOSED {} {} ({:.0f}%) | Entry: {} | Exit: {} | PnL: ${:.2f} ({:.2f}%) | Reason: {}",
            direction, token, close_pct, entry_price, fill_price, pnl_usd, pnl_pct, exit_reason,
        )

        # Update DB if full close
        if close_pct >= 99.9:
            await self.db.close_trade(trade_id, fill_price, pnl_usd, pnl_pct, fees, exit_reason)

        return {
            "trade_id": trade_id,
            "token": token,
            "exit_price": fill_price,
            "pnl_usd": pnl_usd,
            "pnl_pct": pnl_pct,
            "fees": fees,
            "close_pct": close_pct,
            "exit_reason": exit_reason,
        }

    async def close_all_positions(self) -> list[dict]:
        """Close all open positions."""
        results = []
        open_trades = await self.db.get_open_trades()

        for trade in open_trades:
            result = await self.close_position(
                token=trade["token"],
                direction=trade["direction"],
                trade_id=trade["id"],
                entry_price=trade["entry_price"],
                leverage=int(trade["leverage"]),
                size_usd=trade["size_usd"],
                close_pct=100.0,
                exit_reason="close_all",
            )
            if result:
                results.append(result)
            await asyncio.sleep(0.5)

        return results

    async def cancel_all_open_orders(self) -> None:
        """Cancel all open orders on the exchange."""
        try:
            await self.exchange.cancel_all_orders()
            logger.info("Cancelled all open orders")
        except Exception as e:
            logger.error("Failed to cancel all orders: {}", e)
