import pytest
import time
from unittest.mock import AsyncMock, MagicMock, patch
from core.risk_manager import RiskManager


class TestRiskManager:
    def _make_rm(self):
        db = MagicMock()
        db.get_today_trades = AsyncMock(return_value=[])
        db.get_peak_bankroll = AsyncMock(return_value=1000.0)
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
        result = await rm.check_limits(900.0)
        assert result["ok"]

    @pytest.mark.asyncio
    async def test_check_limits_below_min_bankroll(self):
        rm = self._make_rm()
        result = await rm.check_limits(50.0)
        assert not result["ok"]
        assert rm.is_stopped

    @pytest.mark.asyncio
    async def test_check_limits_daily_loss(self):
        rm = self._make_rm()
        rm.db.get_today_trades = AsyncMock(return_value=[
            {"pnl_usd": -50.0},
            {"pnl_usd": -40.0},
        ])
        result = await rm.check_limits(1000.0)
        # -90/1000 = 9% > 8% limit
        assert not result["ok"]

    @pytest.mark.asyncio
    async def test_check_limits_total_drawdown(self):
        rm = self._make_rm()
        rm.db.get_peak_bankroll = AsyncMock(return_value=1000.0)
        result = await rm.check_limits(750.0)
        # 25% drawdown > 20% limit
        assert not result["ok"]
        assert rm.is_stopped
