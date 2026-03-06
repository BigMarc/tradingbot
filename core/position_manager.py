from __future__ import annotations

import time
from dataclasses import dataclass, field
from config.settings import load_strategy
from core.exchange import Exchange
from core.market_data import MarketData
from core.order_executor import OrderExecutor
from core.risk_manager import RiskManager
from storage.database import Database
from utils.logger import logger


@dataclass
class ManagedPosition:
    trade_id: int
    token: str
    direction: str
    entry_price: float
    leverage: int
    size_usd: float
    entry_time: float
    max_price: float = 0.0
    min_price: float = float("inf")
    current_price: float = 0.0
    tp_targets_hit: list[int] = field(default_factory=list)
    remaining_size_pct: float = 100.0
    ai_stop_loss_pct: float = 2.0
    ai_max_hold_minutes: int = 180
    ai_tp_targets: list[dict] = field(default_factory=list)

    @property
    def pnl_pct(self) -> float:
        if self.entry_price <= 0 or self.current_price <= 0:
            return 0.0
        if self.direction == "LONG":
            return ((self.current_price - self.entry_price) / self.entry_price) * 100 * self.leverage
        return ((self.entry_price - self.current_price) / self.entry_price) * 100 * self.leverage

    @property
    def max_pnl_pct(self) -> float:
        if self.entry_price <= 0:
            return 0.0
        if self.direction == "LONG":
            return ((self.max_price - self.entry_price) / self.entry_price) * 100 * self.leverage
        return ((self.entry_price - self.min_price) / self.entry_price) * 100 * self.leverage

    @property
    def hold_minutes(self) -> float:
        return (time.time() - self.entry_time) / 60.0


class PositionManager:
    def __init__(self, exchange: Exchange, market_data: MarketData, executor: OrderExecutor, risk_manager: RiskManager, db: Database) -> None:
        self.exchange = exchange
        self.market_data = market_data
        self.executor = executor
        self.risk_manager = risk_manager
        self.db = db
        self._positions: dict[int, ManagedPosition] = {}

    def add_position(self, trade_id: int, token: str, direction: str, entry_price: float, leverage: int, size_usd: float, ai_decision: dict) -> None:
        pos = ManagedPosition(
            trade_id=trade_id,
            token=token,
            direction=direction,
            entry_price=entry_price,
            leverage=leverage,
            size_usd=size_usd,
            entry_time=time.time(),
            max_price=entry_price,
            min_price=entry_price,
            current_price=entry_price,
            ai_stop_loss_pct=ai_decision.get("stop_loss_pct", 2.0),
            ai_max_hold_minutes=ai_decision.get("max_hold_minutes", 180),
            ai_tp_targets=ai_decision.get("take_profit_targets", []),
        )
        self._positions[trade_id] = pos
        logger.info("Position manager tracking: {} {} (trade #{})", direction, token, trade_id)

    @property
    def positions(self) -> dict[int, ManagedPosition]:
        return self._positions.copy()

    async def check_all_positions(self) -> list[dict]:
        """Check all managed positions and execute stops/TPs. Returns list of close actions taken."""
        actions = []
        strategy = load_strategy()
        ts_config = strategy.get("trading", {}).get("trailing_stop", {})
        zombie_config = strategy.get("trading", {}).get("zombie_protection", {})

        to_remove = []

        for trade_id, pos in self._positions.items():
            mid = self.market_data.get_mid_price(pos.token)
            if not mid:
                continue

            pos.current_price = mid
            pos.max_price = max(pos.max_price, mid)
            pos.min_price = min(pos.min_price, mid)

            action = await self._check_position(pos, ts_config, zombie_config)
            if action:
                actions.append(action)
                if action.get("full_close"):
                    to_remove.append(trade_id)

        for tid in to_remove:
            del self._positions[tid]

        return actions

    async def _check_position(self, pos: ManagedPosition, ts_config: dict, zombie_config: dict) -> dict | None:
        pnl = pos.pnl_pct
        max_pnl = pos.max_pnl_pct

        # 1. Check initial stop-loss
        if pnl <= -pos.ai_stop_loss_pct:
            result = await self.executor.close_position(
                pos.token, pos.direction, pos.trade_id, pos.entry_price,
                pos.leverage, pos.size_usd * (pos.remaining_size_pct / 100),
                100.0, "stop_loss",
            )
            if result:
                self.risk_manager.record_trade_close(result["pnl_usd"], "stop_loss")
                return {**result, "full_close": True}

        # 2. Trailing stop logic
        breakeven_trigger = ts_config.get("breakeven_trigger_pct", 1.0)
        tiers = ts_config.get("tiers", [])

        if max_pnl >= breakeven_trigger:
            # Find the active trailing tier
            trail_pct = 0.0
            for tier in sorted(tiers, key=lambda t: t["profit_pct"], reverse=True):
                if max_pnl >= tier["profit_pct"]:
                    trail_pct = tier["trail_pct"] / 100.0
                    break

            if trail_pct > 0:
                trail_stop_level = max_pnl * (1 - trail_pct)
            else:
                # Breakeven: stop at 0% (entry)
                trail_stop_level = 0.0

            if pnl <= trail_stop_level:
                result = await self.executor.close_position(
                    pos.token, pos.direction, pos.trade_id, pos.entry_price,
                    pos.leverage, pos.size_usd * (pos.remaining_size_pct / 100),
                    100.0, "trailing_stop",
                )
                if result:
                    self.risk_manager.record_trade_close(result["pnl_usd"], "trailing_stop")
                    return {**result, "full_close": True}

        # 3. Take profit targets
        for i, tp in enumerate(pos.ai_tp_targets):
            if i in pos.tp_targets_hit:
                continue
            if pnl >= tp.get("pct", 999):
                close_pct = tp.get("close_pct", 50)
                result = await self.executor.close_position(
                    pos.token, pos.direction, pos.trade_id, pos.entry_price,
                    pos.leverage, pos.size_usd * (pos.remaining_size_pct / 100),
                    close_pct, f"take_profit_{i+1}",
                )
                if result:
                    pos.tp_targets_hit.append(i)
                    pos.remaining_size_pct -= close_pct * (pos.remaining_size_pct / 100)
                    if pos.remaining_size_pct <= 1.0:
                        self.risk_manager.record_trade_close(result["pnl_usd"], f"take_profit_{i+1}")
                        return {**result, "full_close": True}
                    return {**result, "full_close": False}

        # 4. Zombie protection
        max_hold = zombie_config.get("max_hold_minutes", 180)
        min_profit = zombie_config.get("min_profit_for_hold_pct", 0.5)
        force_mult = zombie_config.get("force_close_multiplier", 2)

        if pos.hold_minutes > max_hold and pnl < min_profit:
            result = await self.executor.close_position(
                pos.token, pos.direction, pos.trade_id, pos.entry_price,
                pos.leverage, pos.size_usd * (pos.remaining_size_pct / 100),
                100.0, "zombie_close",
            )
            if result:
                self.risk_manager.record_trade_close(result["pnl_usd"], "zombie_close")
                return {**result, "full_close": True}

        if pos.hold_minutes > max_hold * force_mult:
            result = await self.executor.close_position(
                pos.token, pos.direction, pos.trade_id, pos.entry_price,
                pos.leverage, pos.size_usd * (pos.remaining_size_pct / 100),
                100.0, "force_close",
            )
            if result:
                self.risk_manager.record_trade_close(result["pnl_usd"], "force_close")
                return {**result, "full_close": True}

        return None

    async def sync_from_db(self) -> None:
        """Restore managed positions from database on startup."""
        open_trades = await self.db.get_open_trades()
        for trade in open_trades:
            if trade["id"] not in self._positions:
                self.add_position(
                    trade_id=trade["id"],
                    token=trade["token"],
                    direction=trade["direction"],
                    entry_price=trade["entry_price"],
                    leverage=int(trade["leverage"]),
                    size_usd=trade["size_usd"],
                    ai_decision={
                        "stop_loss_pct": 2.0,
                        "max_hold_minutes": 180,
                        "take_profit_targets": [
                            {"pct": 2.0, "close_pct": 50},
                            {"pct": 4.0, "close_pct": 30},
                        ],
                    },
                )
                logger.info("Restored position from DB: {} {} (trade #{})", trade["direction"], trade["token"], trade["id"])

    async def close_all(self) -> list[dict]:
        """Force close all managed positions."""
        results = await self.executor.close_all_positions()
        self._positions.clear()
        return results
