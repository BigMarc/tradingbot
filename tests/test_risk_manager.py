import pytest
import time
from unittest.mock import AsyncMock, MagicMock, patch
from core.risk_manager import RiskManager


class TestRiskManager:
    def _make_rm(self):
        db = MagicMock()
        db.get_today_trades = AsyncMock(return_value=[])
        db.get_peak_bankroll = AsyncMock(return_value=1000.0)
        db.get_trades_since = AsyncMock(return_value=[])
        db.get_recent_trades = AsyncMock(return_value=[])
        return RiskManager(db)

    def test_initial_state(self):
        rm = self._make_rm()
        assert not rm.is_paused
        assert not rm.is_stopped

    def test_pause_and_resume(self):
        rm = self._make_rm()
        rm.pause(1)
        assert rm.is_paused
        rm.resume()
        assert not rm.is_paused

    def test_stop(self):
        rm = self._make_rm()
        rm.stop()
        assert rm.is_stopped
        rm.resume()
        assert not rm.is_stopped

    def test_can_open_trade_when_stopped(self):
        rm = self._make_rm()
        rm.stop()
        can, reason = rm.can_open_trade()
        assert not can
        assert "stopped" in reason.lower()

    def test_can_open_trade_when_paused(self):
        rm = self._make_rm()
        rm.pause(10)
        can, reason = rm.can_open_trade()
        assert not can
        assert "paused" in reason.lower()

    def test_can_open_trade_normally(self):
        rm = self._make_rm()
        can, reason = rm.can_open_trade()
        assert can
        assert reason == "OK"

    def test_cooldown_after_close(self):
        rm = self._make_rm()
        rm.record_trade_close(10.0, "take_profit")
        can, reason = rm.can_open_trade()
        assert not can
        assert "cooldown" in reason.lower()

    def test_cooldown_after_stoploss(self):
        rm = self._make_rm()
        rm.record_trade_close(-10.0, "stop_loss")
        can, reason = rm.can_open_trade()
        assert not can

    def test_consecutive_losses_tracking(self):
        rm = self._make_rm()
        rm.record_trade_close(-5.0, "stop_loss")
        assert rm._consecutive_losses == 1
        rm.record_trade_close(-5.0, "stop_loss")
        assert rm._consecutive_losses == 2
        rm.record_trade_close(10.0, "take_profit")
        assert rm._consecutive_losses == 0

    def test_validate_trade_params_enforces_limits(self):
        rm = self._make_rm()
        lev, size = rm.validate_trade_params(10, 10.0, 1000.0)
        assert lev <= 5
        assert size <= 5.0

    def test_validate_trade_params_allows_valid(self):
        rm = self._make_rm()
        lev, size = rm.validate_trade_params(3, 3.0, 1000.0)
        assert lev == 3
        assert size == 3.0

    @pytest.mark.asyncio
    async def test_check_limits_ok(self):
        rm = self._make_rm()
        rm.set_bankroll_at_day_start(1000.0)
        result = await rm.check_limits(900.0)
        assert result["ok"]

    @pytest.mark.asyncio
    async def test_check_limits_below_min_bankroll(self):
        rm = self._make_rm()
        rm.set_bankroll_at_day_start(1000.0)
        result = await rm.check_limits(50.0)
        assert not result["ok"]
        assert rm.is_stopped

    @pytest.mark.asyncio
    async def test_check_limits_total_drawdown(self):
        rm = self._make_rm()
        rm.db.get_peak_bankroll = AsyncMock(return_value=1000.0)
        rm.set_bankroll_at_day_start(1000.0)
        result = await rm.check_limits(750.0)
        # 25% drawdown > 20% limit
        assert not result["ok"]
        assert rm.is_stopped


class TestDrawdownShield:
    def _make_rm(self):
        db = MagicMock()
        db.get_today_trades = AsyncMock(return_value=[])
        db.get_peak_bankroll = AsyncMock(return_value=1000.0)
        db.get_trades_since = AsyncMock(return_value=[])
        db.get_recent_trades = AsyncMock(return_value=[])
        return RiskManager(db)

    def test_multiplier_normal(self):
        rm = self._make_rm()
        mult, tier = rm.get_position_size_multiplier(-1.0)
        assert mult == 1.0

    def test_multiplier_half_size(self):
        rm = self._make_rm()
        mult, tier = rm.get_position_size_multiplier(-5.5)
        assert mult == 0.5

    def test_multiplier_quarter_size(self):
        rm = self._make_rm()
        mult, tier = rm.get_position_size_multiplier(-7.5)
        assert mult == 0.25

    def test_multiplier_full_pause(self):
        rm = self._make_rm()
        mult, tier = rm.get_position_size_multiplier(-9.0)
        assert mult == 0.0

    def test_multiplier_at_boundary(self):
        rm = self._make_rm()
        # Exactly at -3.0 should trigger the tier
        mult, tier = rm.get_position_size_multiplier(-3.0)
        assert mult == 1.0

    def test_multiplier_positive_pnl(self):
        rm = self._make_rm()
        mult, tier = rm.get_position_size_multiplier(5.0)
        assert mult == 1.0

    def test_shield_tier_change_detection(self):
        rm = self._make_rm()
        # First set doesn't alert
        assert not rm.check_shield_tier_changed(0)
        # Now it's set; changing should alert
        assert rm.check_shield_tier_changed(1)
        # Same tier - no alert
        assert not rm.check_shield_tier_changed(1)
        # Change again
        assert rm.check_shield_tier_changed(2)

    @pytest.mark.asyncio
    async def test_daily_pnl_pct_initial(self):
        rm = self._make_rm()
        pct = await rm.get_daily_pnl_pct(1000.0)
        # First call sets bankroll_at_day_start, so pnl = 0
        assert pct == 0.0

    @pytest.mark.asyncio
    async def test_daily_pnl_pct_with_loss(self):
        rm = self._make_rm()
        rm.set_bankroll_at_day_start(1000.0)
        rm._last_day_reset = time.time()  # Ensure no day rollover
        rm.db.get_today_trades = AsyncMock(return_value=[
            {"pnl_usd": -30.0},
            {"pnl_usd": -20.0},
        ])
        # Current bankroll is 950 (1000 - 50 realized)
        pct = await rm.get_daily_pnl_pct(950.0)
        assert pct == pytest.approx(-5.0, abs=0.1)


class TestOvertradingDetection:
    def _make_rm(self):
        db = MagicMock()
        db.get_today_trades = AsyncMock(return_value=[])
        db.get_peak_bankroll = AsyncMock(return_value=1000.0)
        db.get_trades_since = AsyncMock(return_value=[])
        db.get_recent_trades = AsyncMock(return_value=[])
        return RiskManager(db)

    @pytest.mark.asyncio
    async def test_no_overtrading(self):
        rm = self._make_rm()
        rm.db.get_trades_since = AsyncMock(return_value=[
            {"exit_time": time.time()},
            {"exit_time": time.time()},
        ])
        is_ot, cd = await rm.check_overtrading()
        assert not is_ot
        assert cd == 0

    @pytest.mark.asyncio
    async def test_overtrading_triggered(self):
        rm = self._make_rm()
        rm.db.get_trades_since = AsyncMock(return_value=[
            {"exit_time": time.time()} for _ in range(7)
        ])
        is_ot, cd = await rm.check_overtrading()
        assert is_ot
        assert cd == 30  # default cooldown

    @pytest.mark.asyncio
    async def test_chop_market_not_enough_data(self):
        rm = self._make_rm()
        rm.db.get_recent_trades = AsyncMock(return_value=[
            {"exit_reason": "stop_loss"} for _ in range(3)
        ])
        is_chop, cd, rate = await rm.check_chop_market()
        assert not is_chop  # Not enough trades (need 10)

    @pytest.mark.asyncio
    async def test_chop_market_triggered(self):
        rm = self._make_rm()
        trades = [{"exit_reason": "stop_loss"} for _ in range(7)]
        trades += [{"exit_reason": "take_profit"} for _ in range(3)]
        rm.db.get_recent_trades = AsyncMock(return_value=trades)
        is_chop, cd, rate = await rm.check_chop_market()
        assert is_chop
        assert cd == 45
        assert rate >= 0.6

    @pytest.mark.asyncio
    async def test_chop_market_not_triggered(self):
        rm = self._make_rm()
        trades = [{"exit_reason": "stop_loss"} for _ in range(3)]
        trades += [{"exit_reason": "take_profit"} for _ in range(7)]
        rm.db.get_recent_trades = AsyncMock(return_value=trades)
        is_chop, cd, rate = await rm.check_chop_market()
        assert not is_chop

    def test_cooldown_state(self):
        rm = self._make_rm()
        in_cd, reason = rm.is_in_cooldown()
        assert not in_cd

        rm._overtrading_cooldown_until = time.time() + 600
        in_cd, reason = rm.is_in_cooldown()
        assert in_cd
        assert "overtrading" in reason.lower()

    def test_resume_clears_cooldowns(self):
        rm = self._make_rm()
        rm._overtrading_cooldown_until = time.time() + 600
        rm._chop_cooldown_until = time.time() + 600
        rm.resume()
        in_cd, _ = rm.is_in_cooldown()
        assert not in_cd
