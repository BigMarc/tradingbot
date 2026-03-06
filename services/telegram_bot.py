from __future__ import annotations

import asyncio
from telegram import Update, Bot
from telegram.ext import Application, CommandHandler, ContextTypes
from config.settings import settings
from utils.logger import logger
from utils.helpers import format_usd, format_pct, ts_to_str


class TelegramBot:
    """Telegram bot for status updates and commands."""

    def __init__(self) -> None:
        self._app: Application | None = None
        self._bot: Bot | None = None
        self._trader = None
        self._optimizer = None
        self._risk_manager = None
        self._position_manager = None
        self._db = None
        self._running = False
        self._health_monitor = None

    def set_health_monitor(self, health_monitor) -> None:
        self._health_monitor = health_monitor

    def set_components(self, trader, optimizer, risk_manager, position_manager, db) -> None:
        self._trader = trader
        self._optimizer = optimizer
        self._risk_manager = risk_manager
        self._position_manager = position_manager
        self._db = db

    async def start(self) -> None:
        if not settings.telegram_bot_token or not settings.telegram_chat_id:
            logger.warning("Telegram not configured, skipping")
            return

        self._running = True
        self._app = Application.builder().token(settings.telegram_bot_token).build()
        self._bot = self._app.bot

        # Register commands
        self._app.add_handler(CommandHandler("status", self._cmd_status))
        self._app.add_handler(CommandHandler("trades", self._cmd_trades))
        self._app.add_handler(CommandHandler("stats", self._cmd_stats))
        self._app.add_handler(CommandHandler("pause", self._cmd_pause))
        self._app.add_handler(CommandHandler("resume", self._cmd_resume))
        self._app.add_handler(CommandHandler("close_all", self._cmd_close_all))
        self._app.add_handler(CommandHandler("params", self._cmd_params))
        self._app.add_handler(CommandHandler("balance", self._cmd_balance))

        await self._app.initialize()
        await self._app.start()
        await self._app.updater.start_polling(drop_pending_updates=True)

        logger.info("Telegram bot started")
        await self.send_message("Trading Bot gestartet!")

        # Keep running
        _tg_hb_counter = 0
        while self._running:
            await asyncio.sleep(1)
            _tg_hb_counter += 1
            if _tg_hb_counter >= 60:
                _tg_hb_counter = 0
                if self._health_monitor:
                    self._health_monitor.heartbeat("telegram")

    async def stop(self) -> None:
        self._running = False
        if self._app:
            await self.send_message("Trading Bot wird gestoppt...")
            await self._app.updater.stop()
            await self._app.stop()
            await self._app.shutdown()

    async def send_message(self, text: str, parse_mode: str = "HTML") -> None:
        if not self._bot or not settings.telegram_chat_id:
            return
        try:
            await self._bot.send_message(
                chat_id=settings.telegram_chat_id,
                text=text,
                parse_mode=parse_mode,
            )
        except Exception as e:
            logger.error("Telegram send error: {}", e)

    # ── Notification Methods ─────────────────────────────────

    async def notify_trade_open(self, token: str, direction: str, price: float, leverage: int, sl: float, tp1: float, size_pct: float) -> None:
        emoji = "\U0001f7e2" if direction == "LONG" else "\U0001f534"
        text = (
            f"{emoji} <b>{direction} {token}</b>\n"
            f"Entry: ${price:.4f} | {leverage}x\n"
            f"SL: ${sl:.4f} | TP1: ${tp1:.4f}\n"
            f"Size: {size_pct:.1f}%"
        )
        await self.send_message(text)

    async def notify_trade_close(self, token: str, direction: str, pnl_usd: float, pnl_pct: float, duration_min: float, reason: str) -> None:
        emoji = "\U0001f7e2" if pnl_usd > 0 else "\U0001f534"
        text = (
            f"{emoji} <b>CLOSED {token}</b>\n"
            f"PnL: {format_usd(pnl_usd)} ({format_pct(pnl_pct)})\n"
            f"Duration: {duration_min:.0f}min | Reason: {reason}"
        )
        await self.send_message(text)

    async def notify_alert(self, message: str) -> None:
        await self.send_message(f"\u26a0\ufe0f <b>ALERT</b>\n{message}")

    async def send_hourly_summary(self, bankroll: float, positions: list, session_pnl: float) -> None:
        pos_text = "Keine" if not positions else "\n".join(
            f"  {p['token']} {p['direction']} {format_pct(p['pnl_pct'])}"
            for p in positions
        )
        text = (
            f"\U0001f4ca <b>Hourly Summary</b>\n"
            f"Bankroll: {format_usd(bankroll)}\n"
            f"Session PnL: {format_usd(session_pnl)}\n"
            f"Positionen:\n{pos_text}"
        )
        await self.send_message(text)

    async def send_daily_summary(self, stats: dict, api_costs: float, trading_fees: float = 0) -> None:
        gross_pnl = stats.get('total_pnl', 0)
        net_pnl = gross_pnl - trading_fees - api_costs
        text = (
            f"\U0001f4c5 <b>Daily Summary</b>\n"
            f"Trades: {stats.get('total_trades', 0)}\n"
            f"Win Rate: {stats.get('win_rate', 0):.1f}%\n\n"
            f"<b>PnL Breakdown:</b>\n"
            f"  Gross PnL: {format_usd(gross_pnl)}\n"
            f"  Trading Fees: -{format_usd(trading_fees)}\n"
            f"  AI Kosten: -{format_usd(api_costs)}\n"
            f"  <b>Net PnL: {format_usd(net_pnl)}</b>"
        )
        if stats.get("consecutive_loss_days", 0) >= 3:
            text += (
                f"\n\n\u26a0\ufe0f <b>Warnung:</b> {stats['consecutive_loss_days']} "
                f"negative Tage in Folge!"
            )
        await self.send_message(text)

    # ── Command Handlers ─────────────────────────────────────

    async def _cmd_status(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        if not self._trader:
            await update.message.reply_text("Bot nicht initialisiert")
            return
        status = self._trader.get_status()
        pos_text = ""
        for tid, p in status.get("positions", {}).items():
            pos_text += f"\n  #{tid} {p['token']} {p['direction']} {format_pct(p['pnl_pct'])} ({p['hold_minutes']:.0f}min)"

        text = (
            f"<b>Status</b>\n"
            f"Running: {status['running']} | Paused: {status['paused']}\n"
            f"Bankroll: {format_usd(status['bankroll'])}\n"
            f"Positionen: {status['open_positions']}/2"
            f"{pos_text}"
        )
        await update.message.reply_text(text, parse_mode="HTML")

    async def _cmd_trades(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        if not self._db:
            return
        trades = await self._db.get_recent_trades(10)
        if not trades:
            await update.message.reply_text("Keine Trades")
            return
        lines = ["<b>Letzte 10 Trades</b>"]
        for t in trades:
            pnl = t.get("pnl_usd", 0)
            emoji = "\u2705" if pnl > 0 else "\u274c"
            lines.append(
                f"{emoji} {t['token']} {t['direction']} {format_usd(pnl)} ({format_pct(t.get('pnl_pct', 0))}) - {t.get('exit_reason', '')}"
            )
        await update.message.reply_text("\n".join(lines), parse_mode="HTML")

    async def _cmd_stats(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        if not self._optimizer:
            return
        stats = await self._optimizer.get_stats()
        text = (
            f"<b>24h Stats</b>\n"
            f"Trades: {stats['total_trades']}\n"
            f"Win Rate: {stats['win_rate']:.1f}%\n"
            f"PnL: {format_usd(stats.get('total_pnl', 0))}\n"
            f"Profit Factor: {stats['profit_factor']:.2f}\n"
            f"Avg Hold: {stats['avg_hold_minutes']:.0f}min\n"
            f"Best: {stats['best_token']} | Worst: {stats['worst_token']}"
        )
        await update.message.reply_text(text, parse_mode="HTML")

    async def _cmd_pause(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        if self._trader:
            self._trader.pause()
            await update.message.reply_text("Bot pausiert. Bestehende Positionen werden weiter gemanaged.")

    async def _cmd_resume(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        if self._trader:
            self._trader.resume()
        if self._risk_manager:
            self._risk_manager.resume()
        await update.message.reply_text("Bot resumed.")

    async def _cmd_close_all(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        if not self._position_manager:
            return
        results = await self._position_manager.close_all()
        await update.message.reply_text(f"Alle Positionen geschlossen ({len(results)} trades)")

    async def _cmd_params(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        from config.settings import load_strategy
        import yaml
        strategy = load_strategy()
        text = f"<pre>{yaml.dump(strategy, default_flow_style=False)[:3000]}</pre>"
        await update.message.reply_text(text, parse_mode="HTML")

    async def _cmd_balance(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        if not self._trader:
            return
        status = self._trader.get_status()
        today_costs = 0
        if self._db:
            today_costs = await self._db.get_today_api_costs()
        text = (
            f"<b>Balance</b>\n"
            f"Bankroll: {format_usd(status['bankroll'])}\n"
            f"AI Kosten heute: {format_usd(today_costs)}"
        )
        await update.message.reply_text(text, parse_mode="HTML")
