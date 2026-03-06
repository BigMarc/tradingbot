from __future__ import annotations

import asyncio
import time
import yaml
from config.settings import load_strategy, save_strategy
from core.ai_brain import AIBrain
from storage.database import Database
from utils.logger import logger


class Optimizer:
    """Periodic performance analysis and parameter optimization."""

    def __init__(self, ai_brain: AIBrain, db: Database) -> None:
        self.ai_brain = ai_brain
        self.db = db
        self._running = False

    async def start(self) -> None:
        self._running = True
        strategy = load_strategy()
        interval_hours = strategy.get("optimizer", {}).get("interval_hours", 4)
        min_trades = strategy.get("optimizer", {}).get("min_trades_for_analysis", 10)

        logger.info("Optimizer starting (interval: {}h, min trades: {})", interval_hours, min_trades)

        while self._running:
            await asyncio.sleep(interval_hours * 3600)
            if not self._running:
                break

            try:
                await self._run_optimization(min_trades)
            except asyncio.CancelledError:
                break
            except Exception as e:
                logger.error("Optimizer error: {}", e)

    async def stop(self) -> None:
        self._running = False

    async def _run_optimization(self, min_trades: int) -> dict | None:
        logger.info("Running optimization cycle...")

        # Phase 1: Statistical Analysis
        since = time.time() - 86400  # Last 24h
        trades = await self.db.get_trades_since(since)

        if len(trades) < min_trades:
            logger.info("Not enough trades for optimization ({}/{})", len(trades), min_trades)
            return None

        stats = self._compute_stats(trades)
        logger.info(
            "Stats: {} trades | WR: {:.1f}% | PF: {:.2f} | Avg hold: {:.0f}min",
            stats["total_trades"], stats["win_rate"], stats["profit_factor"], stats["avg_hold_minutes"],
        )

        # Phase 2: AI Parameter Review
        result = await self.ai_brain.optimize_review(stats)

        if result is None:
            logger.info("Optimizer: NO_CHANGES recommended")
            await self.db.insert_optimizer_log(stats, None, "NO_CHANGES")
            return stats

        # Try to parse and apply YAML changes
        raw = result.get("raw_response", "")
        try:
            changes = yaml.safe_load(raw)
            if isinstance(changes, dict):
                strategy = load_strategy()
                applied = self._apply_changes(strategy, changes)
                if applied:
                    save_strategy(strategy)
                    logger.info("Optimizer applied changes: {}", list(applied.keys()))
                    await self.db.insert_optimizer_log(stats, applied, raw)
                else:
                    await self.db.insert_optimizer_log(stats, None, raw)
            else:
                await self.db.insert_optimizer_log(stats, None, raw)
        except Exception as e:
            logger.warning("Failed to parse optimizer response: {}", e)
            await self.db.insert_optimizer_log(stats, None, raw)

        return stats

    def _compute_stats(self, trades: list[dict]) -> dict:
        if not trades:
            return {
                "total_trades": 0, "win_rate": 0, "avg_win": 0, "avg_loss": 0,
                "profit_factor": 0, "max_drawdown": 0, "avg_hold_minutes": 0,
                "best_token": "N/A", "worst_token": "N/A",
            }

        wins = [t for t in trades if t.get("pnl_usd", 0) > 0]
        losses = [t for t in trades if t.get("pnl_usd", 0) <= 0]

        total_wins = sum(t["pnl_usd"] for t in wins) if wins else 0
        total_losses = abs(sum(t["pnl_usd"] for t in losses)) if losses else 0

        # Hold times
        hold_times = []
        for t in trades:
            if t.get("entry_time") and t.get("exit_time"):
                hold_times.append((t["exit_time"] - t["entry_time"]) / 60.0)

        # Token performance
        token_pnl: dict[str, float] = {}
        for t in trades:
            token_pnl[t["token"]] = token_pnl.get(t["token"], 0) + t.get("pnl_usd", 0)

        best_token = max(token_pnl, key=token_pnl.get, default="N/A")  # type: ignore
        worst_token = min(token_pnl, key=token_pnl.get, default="N/A")  # type: ignore

        # Max drawdown
        cumulative = 0.0
        peak = 0.0
        max_dd = 0.0
        for t in trades:
            cumulative += t.get("pnl_usd", 0)
            peak = max(peak, cumulative)
            dd = peak - cumulative
            max_dd = max(max_dd, dd)

        return {
            "total_trades": len(trades),
            "win_rate": (len(wins) / len(trades) * 100) if trades else 0,
            "avg_win": (total_wins / len(wins)) if wins else 0,
            "avg_loss": -(total_losses / len(losses)) if losses else 0,
            "profit_factor": (total_wins / total_losses) if total_losses > 0 else float("inf"),
            "max_drawdown": max_dd,
            "avg_hold_minutes": (sum(hold_times) / len(hold_times)) if hold_times else 0,
            "best_token": best_token,
            "worst_token": worst_token,
            "total_pnl": sum(t.get("pnl_usd", 0) for t in trades),
            "total_fees": sum(t.get("fees", 0) for t in trades),
        }

    def _apply_changes(self, strategy: dict, changes: dict) -> dict:
        """Apply safe changes to strategy, returning what was changed."""
        applied = {}

        # Only allow certain keys to be changed
        allowed_signal_keys = {"min_score", "weights"}
        allowed_trading_keys = {"trailing_stop", "take_profit", "cooldown"}

        if "signal" in changes and isinstance(changes["signal"], dict):
            for key, value in changes["signal"].items():
                if key in allowed_signal_keys:
                    strategy.setdefault("signal", {})[key] = value
                    applied[f"signal.{key}"] = value

        if "trading" in changes and isinstance(changes["trading"], dict):
            for key, value in changes["trading"].items():
                if key in allowed_trading_keys:
                    strategy.setdefault("trading", {})[key] = value
                    applied[f"trading.{key}"] = value

        return applied

    async def get_stats(self) -> dict:
        """Get current performance stats (for Telegram command)."""
        since = time.time() - 86400
        trades = await self.db.get_trades_since(since)
        return self._compute_stats(trades)
