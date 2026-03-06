from __future__ import annotations

import time
from config.settings import load_strategy, settings
from storage.database import Database
from utils.logger import logger


class RiskManager:
    def __init__(self, db: Database) -> None:
        self.db = db
        self._paused_until: float = 0
        self._stopped = False
        self._consecutive_losses = 0
        self._last_close_time: float = 0
        self._last_stoploss_time: float = 0

    @property
    def is_paused(self) -> bool:
        return time.time() < self._paused_until

    @property
    def is_stopped(self) -> bool:
        return self._stopped

    def pause(self, minutes: float) -> None:
        self._paused_until = time.time() + minutes * 60
        logger.warning("Risk Manager: paused for {:.0f} minutes", minutes)

    def stop(self) -> None:
        self._stopped = True
        logger.critical("Risk Manager: BOT STOPPED - manual restart required")

    def resume(self) -> None:
        self._paused_until = 0
        self._stopped = False
        logger.info("Risk Manager: resumed")

    async def check_limits(self, bankroll: float) -> dict:
        """Check all risk limits. Returns status dict with violations."""
        strategy = load_strategy()
        risk_config = strategy.get("risk", {})

        max_daily_loss = risk_config.get("max_daily_loss_pct", 8.0)
        max_total_dd = risk_config.get("max_total_drawdown_pct", 20.0)
        min_bankroll = risk_config.get("min_bankroll_usd", 100)

        violations = []

        # Check minimum bankroll
        if bankroll < min_bankroll:
            violations.append(f"Bankroll ${bankroll:.2f} below minimum ${min_bankroll}")
            self.stop()

        # Check daily loss
        today_trades = await self.db.get_today_trades()
        daily_pnl = sum(t.get("pnl_usd", 0) for t in today_trades)
        daily_loss_pct = abs(daily_pnl / bankroll * 100) if bankroll > 0 and daily_pnl < 0 else 0

        if daily_loss_pct >= max_daily_loss:
            violations.append(f"Daily loss {daily_loss_pct:.1f}% >= {max_daily_loss}% limit")
            self.pause(240)  # 4 hours

        # Check total drawdown from peak
        peak = await self.db.get_peak_bankroll()
        if peak > 0:
            drawdown_pct = ((peak - bankroll) / peak) * 100
            if drawdown_pct >= max_total_dd:
                violations.append(f"Total drawdown {drawdown_pct:.1f}% >= {max_total_dd}% limit")
                self.stop()

        return {
            "ok": len(violations) == 0,
            "violations": violations,
            "daily_pnl": daily_pnl,
            "daily_loss_pct": daily_loss_pct,
            "bankroll": bankroll,
        }

    def can_open_trade(self) -> tuple[bool, str]:
        """Check if a new trade can be opened based on cooldowns and state."""
        if self._stopped:
            return False, "Bot is stopped"
        if self.is_paused:
            remaining = (self._paused_until - time.time()) / 60
            return False, f"Paused for {remaining:.0f} more minutes"

        strategy = load_strategy()
        cooldown = strategy.get("trading", {}).get("cooldown", {})

        now = time.time()

        # Cooldown after close
        close_cooldown = cooldown.get("after_close_minutes", 5) * 60
        if now - self._last_close_time < close_cooldown:
            remaining = (self._last_close_time + close_cooldown - now) / 60
            return False, f"Cooldown after close: {remaining:.1f}min remaining"

        # Cooldown after stoploss
        sl_cooldown = cooldown.get("after_stoploss_minutes", 15) * 60
        if now - self._last_stoploss_time < sl_cooldown:
            remaining = (self._last_stoploss_time + sl_cooldown - now) / 60
            return False, f"Cooldown after stoploss: {remaining:.1f}min remaining"

        # Cooldown after 3 consecutive losses
        loss_cooldown = cooldown.get("after_3_losses_minutes", 60) * 60
        if self._consecutive_losses >= 3:
            if now - self._last_close_time < loss_cooldown:
                remaining = (self._last_close_time + loss_cooldown - now) / 60
                return False, f"3 losses cooldown: {remaining:.1f}min remaining"
            else:
                self._consecutive_losses = 0

        return True, "OK"

    def record_trade_close(self, pnl_usd: float, exit_reason: str) -> None:
        self._last_close_time = time.time()
        if pnl_usd <= 0:
            self._consecutive_losses += 1
            if "stop" in exit_reason.lower():
                self._last_stoploss_time = time.time()
        else:
            self._consecutive_losses = 0

    def validate_trade_params(self, leverage: int, size_pct: float, bankroll: float) -> tuple[int, float]:
        """Enforce hard limits on leverage and position size."""
        strategy = load_strategy()
        trading = strategy.get("trading", {})

        max_lev = min(trading.get("max_leverage", 5), 5)
        max_size = min(trading.get("max_position_size_pct", 5.0), 5.0)

        leverage = max(1, min(leverage, max_lev))
        size_pct = max(0.5, min(size_pct, max_size))

        return leverage, size_pct
