from __future__ import annotations

import asyncio
import time
from config.settings import load_strategy
from core.signal_engine import SignalEngine
from core.ai_brain import AIBrain
from core.order_executor import OrderExecutor
from core.position_manager import PositionManager
from core.risk_manager import RiskManager
from core.exchange import Exchange
from storage.database import Database
from utils.logger import logger
from utils.helpers import format_usd, format_pct


class Trader:
    """Main trading loop: Signal -> AI -> Execute -> Manage."""

    def __init__(
        self,
        exchange: Exchange,
        signal_engine: SignalEngine,
        ai_brain: AIBrain,
        executor: OrderExecutor,
        position_manager: PositionManager,
        risk_manager: RiskManager,
        db: Database,
    ) -> None:
        self.exchange = exchange
        self.signal_engine = signal_engine
        self.ai_brain = ai_brain
        self.executor = executor
        self.position_manager = position_manager
        self.risk_manager = risk_manager
        self.db = db
        self._running = False
        self._paused = False
        self._bankroll: float = 0
        self._telegram = None  # Set via set_telegram()

    def set_telegram(self, telegram_bot) -> None:
        self._telegram = telegram_bot

    @property
    def is_paused(self) -> bool:
        return self._paused

    def pause(self) -> None:
        self._paused = True
        logger.info("Trader paused - no new trades will be opened")

    def resume(self) -> None:
        self._paused = False
        logger.info("Trader resumed")

    async def start(self) -> None:
        self._running = True
        logger.info("Trader service starting...")

        # Get initial bankroll
        try:
            balance = await self.exchange.fetch_balance()
            self._bankroll = float(balance.get("total", {}).get("USDC", 0))
            logger.info("Initial bankroll: {}", format_usd(self._bankroll))
        except Exception as e:
            logger.error("Failed to fetch initial balance: {}", e)
            from config.settings import settings
            self._bankroll = settings.initial_bankroll

        # Set day-start bankroll for drawdown shield
        self.risk_manager.set_bankroll_at_day_start(self._bankroll)

        # Restore positions from DB
        await self.position_manager.sync_from_db()

        # Run signal scanning and position management concurrently
        await asyncio.gather(
            self._signal_loop(),
            self._position_loop(),
            self._bankroll_update_loop(),
        )

    async def stop(self) -> None:
        self._running = False

    async def _signal_loop(self) -> None:
        """Scan for signals every 30 seconds."""
        strategy = load_strategy()
        interval = strategy.get("signal", {}).get("check_interval_seconds", 30)

        while self._running:
            try:
                if not self._paused:
                    await self._process_signals()
            except asyncio.CancelledError:
                break
            except Exception as e:
                logger.error("Signal loop error: {}", e)

            await asyncio.sleep(interval)

    async def _position_loop(self) -> None:
        """Check positions every 10 seconds."""
        while self._running:
            try:
                actions = await self.position_manager.check_all_positions()
                for action in actions:
                    if action.get("full_close"):
                        await self._update_bankroll()
            except asyncio.CancelledError:
                break
            except Exception as e:
                logger.error("Position loop error: {}", e)

            await asyncio.sleep(10)

    async def _bankroll_update_loop(self) -> None:
        """Update bankroll from exchange every 60 seconds."""
        while self._running:
            try:
                await asyncio.sleep(60)
                await self._update_bankroll()
            except asyncio.CancelledError:
                break
            except Exception as e:
                logger.debug("Bankroll update error: {}", e)

    async def _update_bankroll(self) -> None:
        try:
            balance = await self.exchange.fetch_balance()
            self._bankroll = float(balance.get("total", {}).get("USDC", 0))

            # Check risk limits
            risk_status = await self.risk_manager.check_limits(self._bankroll)
            if not risk_status["ok"]:
                for v in risk_status["violations"]:
                    logger.critical("RISK VIOLATION: {}", v)
                    if self._telegram:
                        await self._telegram.notify_alert(v)
                if self.risk_manager.is_stopped:
                    await self.position_manager.close_all()

            # Save snapshot
            positions_data = [
                {"token": p.token, "direction": p.direction, "pnl_pct": p.pnl_pct}
                for p in self.position_manager.positions.values()
            ]
            unrealized = sum(p.pnl_pct * p.size_usd / 100 for p in self.position_manager.positions.values())
            await self.db.insert_snapshot(self._bankroll, unrealized, positions_data)
        except Exception as e:
            logger.debug("Bankroll update failed: {}", e)

    async def _send_telegram(self, text: str) -> None:
        if self._telegram:
            try:
                await self._telegram.send_message(text)
            except Exception:
                pass

    async def _process_signals(self) -> None:
        """Process signals with the correct 6-check ordering.

        Order (cheapest first to save AI tokens):
        1. Overtrading / Chop cooldown (memory check)
        2. Drawdown Shield (1 DB query)
        3. Max positions + risk limits
        4. Cooldown after trade
        5. AI Brain call (most expensive)
        6. Apply drawdown multiplier to size
        """

        # ── CHECK 1: Overtrading / Chop Cooldown ────────────
        in_cooldown, cooldown_reason = self.risk_manager.is_in_cooldown()
        if in_cooldown:
            logger.debug("In cooldown: {}", cooldown_reason)
            return

        is_overtrading, ot_cd = await self.risk_manager.check_overtrading()
        if is_overtrading:
            msg = f"\u23f8\ufe0f Overtrading: >{load_strategy().get('risk', {}).get('overtrading', {}).get('max_trades_per_hour', 6)} Trades/h. Pause fuer {ot_cd}min."
            logger.warning(msg)
            await self._send_telegram(msg)
            return

        is_chop, chop_cd, sl_rate = await self.risk_manager.check_chop_market()
        if is_chop:
            lookback = load_strategy().get("risk", {}).get("chop_market", {}).get("lookback_trades", 10)
            msg = f"\u23f8\ufe0f Chop Market: {sl_rate:.0%} Stop Losses in letzten {lookback} Trades. Pause fuer {chop_cd}min."
            logger.warning(msg)
            await self._send_telegram(msg)
            return

        # ── CHECK 2: Drawdown Shield ────────────────────────
        daily_pnl_pct = await self.risk_manager.get_daily_pnl_pct(self._bankroll)
        multiplier, tier_idx = self.risk_manager.get_position_size_multiplier(daily_pnl_pct)

        if self.risk_manager.check_shield_tier_changed(tier_idx):
            strategy = load_strategy()
            tiers = strategy.get("risk", {}).get("drawdown_shield", {}).get("tiers", [])
            if tier_idx < len(tiers):
                tier = tiers[tier_idx]
                msg = (
                    f"\u26a0\ufe0f Drawdown Shield Stufe {tier_idx + 1}: "
                    f"Tages-PnL bei {daily_pnl_pct:.1f}%. "
                    f"Size auf {multiplier * 100:.0f}% reduziert."
                )
                if multiplier == 0.0:
                    pause_h = strategy.get("risk", {}).get("drawdown_shield", {}).get("pause_duration_hours", 4)
                    msg = (
                        f"\U0001f6d1 Drawdown Shield Stufe {tier_idx + 1}: "
                        f"Tages-PnL bei {daily_pnl_pct:.1f}%. "
                        f"Trading pausiert fuer {pause_h}h."
                    )
                logger.warning(msg)
                await self._send_telegram(msg)

        if multiplier == 0.0:
            logger.warning("Drawdown Shield: trading paused (daily PnL: {:.1f}%)", daily_pnl_pct)
            return

        # ── CHECK 3: Max positions + risk state ─────────────
        strategy = load_strategy()
        max_positions = strategy.get("trading", {}).get("max_positions", 2)
        open_trades = await self.db.get_open_trades()
        if len(open_trades) >= max_positions:
            return

        # ── CHECK 4: Trade cooldowns ────────────────────────
        can_trade, reason = self.risk_manager.can_open_trade()
        if not can_trade:
            logger.debug("Cannot trade: {}", reason)
            return

        # Scan for signals
        signals = await self.signal_engine.scan_for_signals()
        if not signals:
            return

        # Process best signal
        signal = signals[0]
        logger.info("Processing signal: {} {} (score: {:.1f})", signal.direction, signal.token, signal.score)

        # Get context for AI
        today_trades = await self.db.get_today_trades()
        recent_trades = await self.db.get_recent_trades(3)
        btc_trend = self.signal_engine.get_btc_trend()
        market_sentiment = self.signal_engine.get_market_sentiment()
        top_movers = self.signal_engine.get_top_movers()

        # ── CHECK 5: AI Brain Call (most expensive) ─────────
        decision = await self.ai_brain.evaluate_signal(
            signal=signal,
            bankroll=self._bankroll,
            open_positions=open_trades,
            today_trades=today_trades,
            recent_trades=recent_trades,
            btc_trend=btc_trend,
            market_sentiment=market_sentiment,
            top_movers=top_movers,
        )

        if not decision:
            return

        action = decision.get("action", "SKIP")
        if action == "SKIP":
            return

        # Validate params through risk manager
        leverage, size_pct = self.risk_manager.validate_trade_params(
            decision.get("leverage", 3),
            decision.get("position_size_pct", 3.0),
            self._bankroll,
        )

        # ── CHECK 6: Apply drawdown shield multiplier ───────
        original_size = size_pct
        size_pct = size_pct * multiplier

        logger.info(
            "Size Multiplier: {}x | Original: {:.1f}% | Adjusted: {:.1f}%",
            multiplier, original_size, size_pct,
        )

        if size_pct < 0.5:
            logger.info("Adjusted size too small ({:.2f}%), skipping trade", size_pct)
            return

        size_usd = self._bankroll * (size_pct / 100.0)

        # Execute the trade
        result = await self.executor.open_position(
            token=signal.token,
            direction=action,
            leverage=leverage,
            size_usd=size_usd,
            entry_type=decision.get("entry_type", "MARKET"),
            limit_price=decision.get("limit_price"),
            ai_reasoning=decision.get("reasoning", ""),
        )

        if result:
            # Register with position manager
            self.position_manager.add_position(
                trade_id=result["trade_id"],
                token=signal.token,
                direction=action,
                entry_price=result["entry_price"],
                leverage=leverage,
                size_usd=size_usd,
                ai_decision=decision,
            )

            # Update signal with AI decision
            await self.db.insert_signal(signal.token, signal.direction, signal.score, signal.indicators, decision)

            # Notify telegram
            if self._telegram:
                tp_targets = decision.get("take_profit_targets", [])
                tp1_pct = tp_targets[0].get("pct", 0) if tp_targets else 0
                sl_pct = decision.get("stop_loss_pct", 0)
                entry = result["entry_price"]
                if action == "LONG":
                    sl_price = entry * (1 - sl_pct / 100)
                    tp1_price = entry * (1 + tp1_pct / 100)
                else:
                    sl_price = entry * (1 + sl_pct / 100)
                    tp1_price = entry * (1 - tp1_pct / 100)
                await self._telegram.notify_trade_open(
                    signal.token, action, entry, leverage, sl_price, tp1_price, size_pct,
                )

    def get_status(self) -> dict:
        daily_multiplier = 1.0
        try:
            # Sync method - just return cached tier
            _, tier_idx = self.risk_manager.get_position_size_multiplier(0)
            daily_multiplier = 1.0  # Will be updated properly in async context
        except Exception:
            pass

        return {
            "running": self._running,
            "paused": self._paused,
            "bankroll": self._bankroll,
            "open_positions": len(self.position_manager.positions),
            "positions": {
                tid: {
                    "token": p.token,
                    "direction": p.direction,
                    "entry_price": p.entry_price,
                    "current_price": p.current_price,
                    "pnl_pct": p.pnl_pct,
                    "hold_minutes": p.hold_minutes,
                }
                for tid, p in self.position_manager.positions.items()
            },
        }
