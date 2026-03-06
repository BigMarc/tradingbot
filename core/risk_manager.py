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
        # Drawdown shield state
        self._bankroll_at_day_start: float = 0
        self._last_day_reset: float = 0
        self._last_shield_tier: int = -1  # Track tier changes for alerts
        # Overtrading / Chop cooldown state (memory only, resets on restart)
        self._overtrading_cooldown_until: float = 0
        self._chop_cooldown_until: float = 0

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
        self._overtrading_cooldown_until = 0
        self._chop_cooldown_until = 0
        logger.info("Risk Manager: resumed")

    def set_bankroll_at_day_start(self, bankroll: float) -> None:
        self._bankroll_at_day_start = bankroll
        self._last_day_reset = time.time()
        self._last_shield_tier = -1
        logger.info("Drawdown Shield: day-start bankroll set to ${:.2f}", bankroll)

    # ── Drawdown Shield (Extension 1) ────────────────────────

    async def get_daily_pnl_pct(self, current_bankroll: float) -> float:
        """Calculate today's PnL percentage relative to day-start bankroll."""
        if self._bankroll_at_day_start <= 0:
            self._bankroll_at_day_start = current_bankroll
            return 0.0

        # Check for UTC day rollover
        now = time.time()
        day_start_ts = now - (now % 86400)
        if self._last_day_reset < day_start_ts:
            self._bankroll_at_day_start = current_bankroll
            self._last_day_reset = now
            self._last_shield_tier = -1
            return 0.0

        # Realized PnL from today's closed trades
        today_trades = await self.db.get_today_trades()
        realized = sum(t.get("pnl_usd", 0) for t in today_trades)

        # Unrealized PnL: difference between current bankroll and day-start minus realized
        # This is a conservative estimate; direct unrealized is calculated by the caller
        daily_pnl = realized + (current_bankroll - self._bankroll_at_day_start - realized)
        daily_pnl_pct = (daily_pnl / self._bankroll_at_day_start) * 100
        return daily_pnl_pct

    def get_position_size_multiplier(self, daily_pnl_pct: float) -> tuple[float, int]:
        """Returns (multiplier 0.0-1.0, tier_index) based on drawdown shield tiers.

        Tier index is used for alerting on tier changes.
        """
        strategy = load_strategy()
        shield_config = strategy.get("risk", {}).get("drawdown_shield", {})

        if not shield_config.get("enabled", True):
            return 1.0, 0

        tiers = shield_config.get("tiers", [
            {"drawdown_pct": -3.0, "size_multiplier": 1.0},
            {"drawdown_pct": -5.0, "size_multiplier": 0.5},
            {"drawdown_pct": -7.0, "size_multiplier": 0.25},
            {"drawdown_pct": -8.0, "size_multiplier": 0.0},
        ])

        # Validate tiers are sorted descending by drawdown_pct
        for i in range(1, len(tiers)):
            if tiers[i]["drawdown_pct"] > tiers[i - 1]["drawdown_pct"]:
                logger.error("Drawdown shield tiers not sorted descending! Check strategy.yaml")
                break

        # Find active tier (tiers sorted from least to most severe)
        multiplier = 1.0
        tier_idx = 0
        for i, tier in enumerate(tiers):
            if daily_pnl_pct <= tier["drawdown_pct"]:
                multiplier = tier["size_multiplier"]
                tier_idx = i

        return multiplier, tier_idx

    def check_shield_tier_changed(self, tier_idx: int) -> bool:
        """Returns True if the drawdown shield tier changed (for alerting)."""
        if tier_idx != self._last_shield_tier:
            old = self._last_shield_tier
            self._last_shield_tier = tier_idx
            return old >= 0  # Don't alert on initial set
        return False

    # ── Overtrading Detection (Extension 2) ──────────────────

    def is_in_cooldown(self) -> tuple[bool, str]:
        """Check if we're in an overtrading or chop market cooldown."""
        now = time.time()
        if now < self._overtrading_cooldown_until:
            remaining = (self._overtrading_cooldown_until - now) / 60
            return True, f"Overtrading cooldown: {remaining:.0f}min remaining"
        if now < self._chop_cooldown_until:
            remaining = (self._chop_cooldown_until - now) / 60
            return True, f"Chop market cooldown: {remaining:.0f}min remaining"
        return False, ""

    async def check_overtrading(self) -> tuple[bool, int]:
        """Check if too many trades in the last hour. Returns (is_overtrading, cooldown_minutes)."""
        strategy = load_strategy()
        ot_config = strategy.get("risk", {}).get("overtrading", {})

        if not ot_config.get("enabled", True):
            return False, 0

        max_trades = ot_config.get("max_trades_per_hour", 6)
        cooldown_min = ot_config.get("cooldown_minutes", 30)

        one_hour_ago = time.time() - 3600
        trades = await self.db.get_trades_since(one_hour_ago)
        if len(trades) >= max_trades:
            self._overtrading_cooldown_until = time.time() + cooldown_min * 60
            logger.warning("Overtrading detected: {} trades in last hour (max: {})", len(trades), max_trades)
            return True, cooldown_min

        return False, 0

    async def check_chop_market(self) -> tuple[bool, int, float]:
        """Check if recent trades show chop market pattern.

        Returns (is_chop, cooldown_minutes, stoploss_rate).
        """
        strategy = load_strategy()
        chop_config = strategy.get("risk", {}).get("chop_market", {})

        if not chop_config.get("enabled", True):
            return False, 0, 0.0

        lookback = chop_config.get("lookback_trades", 10)
        sl_threshold = chop_config.get("stoploss_rate_threshold", 0.6)
        cooldown_min = chop_config.get("cooldown_minutes", 45)

        recent = await self.db.get_recent_trades(lookback)
        if len(recent) < lookback:
            return False, 0, 0.0

        stop_losses = sum(1 for t in recent if t.get("exit_reason", "").lower().startswith("stop"))
        sl_rate = stop_losses / len(recent)

        if sl_rate >= sl_threshold:
            self._chop_cooldown_until = time.time() + cooldown_min * 60
            logger.warning("Chop market detected: {:.0%} stop losses in last {} trades", sl_rate, lookback)
            return True, cooldown_min, sl_rate

        return False, 0, sl_rate

    # ── Existing Risk Checks ─────────────────────────────────

    async def check_limits(self, bankroll: float) -> dict:
        """Check all risk limits. Returns status dict with violations."""
        strategy = load_strategy()
        risk_config = strategy.get("risk", {})

        max_total_dd = risk_config.get("max_total_drawdown_pct", 20.0)
        min_bankroll = risk_config.get("min_bankroll_usd", 100)

        violations = []

        # Check minimum bankroll
        if bankroll < min_bankroll:
            violations.append(f"Bankroll ${bankroll:.2f} below minimum ${min_bankroll}")
            self.stop()

        # Check total drawdown from peak
        peak = await self.db.get_peak_bankroll()
        if peak > 0:
            drawdown_pct = ((peak - bankroll) / peak) * 100
            if drawdown_pct >= max_total_dd:
                violations.append(f"Total drawdown {drawdown_pct:.1f}% >= {max_total_dd}% limit")
                self.stop()

        # Drawdown shield check (replaces old daily loss hard cutoff)
        daily_pnl_pct = await self.get_daily_pnl_pct(bankroll)
        multiplier, tier_idx = self.get_position_size_multiplier(daily_pnl_pct)
        if multiplier == 0.0:
            shield_config = strategy.get("risk", {}).get("drawdown_shield", {})
            pause_hours = shield_config.get("pause_duration_hours", 4)
            violations.append(f"Drawdown Shield: daily PnL {daily_pnl_pct:.1f}%, trading paused for {pause_hours}h")
            self.pause(pause_hours * 60)

        return {
            "ok": len(violations) == 0,
            "violations": violations,
            "daily_pnl_pct": daily_pnl_pct,
            "size_multiplier": multiplier,
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
