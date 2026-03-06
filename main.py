from __future__ import annotations

import asyncio
import signal
import sys
from utils.logger import logger


def main_entry() -> None:
    """Entry point for the trading bot."""
    try:
        import uvloop
        uvloop.install()
        logger.info("uvloop installed")
    except ImportError:
        logger.warning("uvloop not available, using default event loop")

    asyncio.run(main())


def _log_api_key_reminder() -> None:
    """Log a reminder about API key expiry on startup."""
    logger.info(
        "Reminder: Hyperliquid API keys expire after 180 days. "
        "Rotate your key regularly to avoid unexpected failures."
    )


async def _reconcile_positions(exchange, db) -> None:
    """Reconcile DB open trades with actual exchange positions on startup."""
    try:
        exchange_positions = await exchange.fetch_positions()
        exchange_open_orders = await exchange.fetch_open_orders()

        # Get tokens with actual positions on exchange
        live_tokens = set()
        for pos in exchange_positions:
            contracts = abs(float(pos.get("contracts", 0) or 0))
            if contracts > 0:
                symbol = pos.get("symbol", "")
                base = symbol.split("/")[0] if "/" in symbol else ""
                if base:
                    live_tokens.add(base)

        # Check DB open trades against exchange state
        db_open = await db.get_open_trades()
        for trade in db_open:
            token = trade["token"]
            if token not in live_tokens:
                logger.warning(
                    "Stale DB trade #{} {} {} not found on exchange, closing as reconciled",
                    trade["id"], trade["direction"], token,
                )
                await db.close_trade(
                    trade["id"], trade["entry_price"], 0.0, 0.0, 0.0, "reconcile_startup",
                )

        # Log any exchange orders found
        if exchange_open_orders:
            logger.info("Found {} open orders on exchange at startup", len(exchange_open_orders))

        logger.info(
            "Position reconciliation complete: {} exchange positions, {} DB open trades",
            len(live_tokens), len(db_open),
        )
    except Exception as e:
        logger.warning("Position reconciliation failed (non-fatal): {}", e)


async def main() -> None:
    logger.info("=" * 60)
    logger.info("Hyperliquid Trading Bot Starting...")
    logger.info("=" * 60)

    from config.settings import settings, load_strategy
    from storage.database import Database
    from core.exchange import Exchange
    from core.market_data import MarketData
    from core.signal_engine import SignalEngine
    from core.ai_brain import AIBrain
    from core.order_executor import OrderExecutor
    from core.position_manager import PositionManager
    from core.risk_manager import RiskManager
    from services.data_collector import DataCollector
    from services.trader import Trader
    from services.optimizer import Optimizer
    from services.telegram_bot import TelegramBot
    from services.health_monitor import HealthMonitor

    # 0. Mainnet safety gate
    if settings.network == "mainnet" and not settings.is_mainnet_confirmed:
        logger.critical(
            "MAINNET mode requires CONFIRM_MAINNET=true in .env. "
            "This is a safety measure to prevent accidental mainnet trading. Exiting."
        )
        return

    # 1. Load config
    strategy = load_strategy()
    logger.info("Network: {}", settings.network)
    logger.info("Strategy loaded (signal threshold: {})", strategy.get("signal", {}).get("min_score", 65))

    # 2. Initialize database
    db = Database()
    await db.connect()

    # 3. Initialize exchange
    exchange = Exchange()
    try:
        await exchange.connect()
        balance = await exchange.fetch_balance()
        usdc_balance = balance.get("total", {}).get("USDC", 0)
        logger.info("Exchange connected. Balance: ${:.2f} USDC", float(usdc_balance))
    except Exception as e:
        logger.critical("Failed to connect to exchange: {}", e)
        await db.close()
        return

    # 4. Reconcile positions on startup
    await _reconcile_positions(exchange, db)

    # 5. Initialize components
    market_data = MarketData(exchange, db)
    signal_engine = SignalEngine(exchange, market_data, db)
    ai_brain = AIBrain(db)
    risk_manager = RiskManager(db)
    executor = OrderExecutor(exchange, db)
    position_manager = PositionManager(exchange, market_data, executor, risk_manager, db)

    data_collector = DataCollector(exchange, market_data, db)
    trader = Trader(exchange, signal_engine, ai_brain, executor, position_manager, risk_manager, db)
    optimizer = Optimizer(ai_brain, db)
    telegram_bot = TelegramBot()
    health_monitor = HealthMonitor()

    # Wire up telegram with components
    telegram_bot.set_components(trader, optimizer, risk_manager, position_manager, db)
    trader.set_telegram(telegram_bot)
    ai_brain.set_telegram(telegram_bot)
    health_monitor.set_telegram(telegram_bot)
    health_monitor.set_exchange(exchange)
    health_monitor.set_db(db)
    data_collector.set_health_monitor(health_monitor)
    trader.set_health_monitor(health_monitor)
    optimizer.set_health_monitor(health_monitor)
    telegram_bot.set_health_monitor(health_monitor)

    # 6. API key expiry warning (Hyperliquid keys valid 180 days)
    _log_api_key_reminder()

    # 7. Setup graceful shutdown
    shutdown_event = asyncio.Event()

    def _shutdown_handler(sig, frame):
        logger.info("Received signal {}, initiating graceful shutdown...", sig)
        shutdown_event.set()

    signal.signal(signal.SIGTERM, _shutdown_handler)
    signal.signal(signal.SIGINT, _shutdown_handler)

    # 6. Start all services as tasks
    tasks: dict[str, asyncio.Task] = {}

    tasks["data_collector"] = asyncio.create_task(data_collector.start(), name="data_collector")
    # Give data collector time to connect
    await asyncio.sleep(5)

    tasks["trader"] = asyncio.create_task(trader.start(), name="trader")
    tasks["optimizer"] = asyncio.create_task(optimizer.start(), name="optimizer")
    tasks["telegram"] = asyncio.create_task(telegram_bot.start(), name="telegram")
    tasks["health_monitor"] = asyncio.create_task(health_monitor.start(), name="health_monitor")

    # Register tasks with health monitor
    for name, task in tasks.items():
        health_monitor.register_task(name, task)

    logger.info("All services started. Bot is running.")

    # 7. Wait for shutdown signal or task failure
    try:
        done = asyncio.Event()

        async def _watch_shutdown():
            await shutdown_event.wait()
            done.set()

        async def _watch_tasks():
            while not done.is_set():
                for name, task in tasks.items():
                    if task.done() and not task.cancelled():
                        exc = task.exception()
                        if exc:
                            logger.error("Task {} failed: {}", name, exc)
                            # Restart the task
                            if name == "data_collector":
                                tasks[name] = asyncio.create_task(data_collector.start(), name=name)
                            elif name == "trader":
                                tasks[name] = asyncio.create_task(trader.start(), name=name)
                            elif name == "optimizer":
                                tasks[name] = asyncio.create_task(optimizer.start(), name=name)
                            elif name == "telegram":
                                tasks[name] = asyncio.create_task(telegram_bot.start(), name=name)
                            elif name == "health_monitor":
                                tasks[name] = asyncio.create_task(health_monitor.start(), name=name)
                            health_monitor.register_task(name, tasks[name])
                            logger.info("Restarted task: {}", name)
                await asyncio.sleep(5)

        watch_shutdown = asyncio.create_task(_watch_shutdown())
        watch_tasks = asyncio.create_task(_watch_tasks())

        await done.wait()

    except asyncio.CancelledError:
        pass

    # 8. Graceful shutdown
    logger.info("Shutting down...")

    # Cancel open orders but don't close positions
    try:
        await executor.cancel_all_open_orders()
    except Exception as e:
        logger.error("Error cancelling orders: {}", e)

    # Send shutdown message
    try:
        await telegram_bot.send_message("Bot shutting down. Offene Positionen bleiben offen.")
    except Exception:
        pass

    # Stop all services
    await data_collector.stop()
    await trader.stop()
    await optimizer.stop()
    await telegram_bot.stop()
    await health_monitor.stop()

    # Cancel all tasks
    for task in tasks.values():
        task.cancel()
    watch_shutdown.cancel()
    watch_tasks.cancel()

    await asyncio.gather(*tasks.values(), watch_shutdown, watch_tasks, return_exceptions=True)

    # Close database
    await db.close()

    logger.info("Bot shutdown complete.")


if __name__ == "__main__":
    main_entry()
