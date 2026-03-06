from __future__ import annotations

import asyncio
import os
import shutil
import time
from pathlib import Path
from utils.logger import logger


class HealthMonitor:
    """Watchdog that monitors all running tasks, clock drift, API keys, and DB backups."""

    def __init__(self) -> None:
        self._tasks: dict[str, asyncio.Task] = {}
        self._last_heartbeat: dict[str, float] = {}
        self._running = False
        self._telegram_bot = None
        self._exchange = None
        self._db = None
        self._last_key_check: float = 0
        self._last_backup: float = 0
        self._ws_disconnect_count: int = 0
        self._ws_disconnect_window_start: float = 0
        self._maintenance_mode: bool = False

    def set_telegram(self, telegram_bot) -> None:
        self._telegram_bot = telegram_bot

    def set_exchange(self, exchange) -> None:
        self._exchange = exchange

    def set_db(self, db) -> None:
        self._db = db

    def register_task(self, name: str, task: asyncio.Task) -> None:
        self._tasks[name] = task
        self._last_heartbeat[name] = time.time()

    def heartbeat(self, name: str) -> None:
        self._last_heartbeat[name] = time.time()

    def record_ws_disconnect(self) -> None:
        """Track WebSocket disconnects for maintenance mode detection."""
        now = time.time()
        # Reset window every 10 minutes
        if now - self._ws_disconnect_window_start > 600:
            self._ws_disconnect_count = 0
            self._ws_disconnect_window_start = now
        self._ws_disconnect_count += 1

    @property
    def is_maintenance_mode(self) -> bool:
        return self._maintenance_mode

    async def start(self) -> None:
        self._running = True
        logger.info("Health monitor starting...")

        while self._running:
            await asyncio.sleep(60)
            if not self._running:
                break
            self._last_heartbeat["health_monitor"] = time.time()
            await self._check_health()
            await self._check_maintenance_mode()
            await self._check_clock_drift()
            await self._check_db_backup()

    async def stop(self) -> None:
        self._running = False

    async def _check_health(self) -> None:
        now = time.time()
        issues = []

        for name, task in self._tasks.items():
            if task.done():
                exc = task.exception() if not task.cancelled() else None
                if exc:
                    issues.append(f"{name}: CRASHED ({type(exc).__name__}: {exc})")
                    logger.error("Task {} crashed: {}", name, exc)
                elif task.cancelled():
                    issues.append(f"{name}: CANCELLED")
                    logger.warning("Task {} was cancelled", name)

        # Check heartbeats (allow 5 minutes of silence)
        for name, last in self._last_heartbeat.items():
            task = self._tasks.get(name)
            if task and task.done():
                continue  # Skip heartbeat check for finished tasks
            if now - last > 300:
                issues.append(f"{name}: no heartbeat for {(now - last) / 60:.0f}min")

        if issues:
            alert_msg = "Health issues:\n" + "\n".join(f"- {i}" for i in issues)
            logger.warning(alert_msg)
            if self._telegram_bot:
                await self._telegram_bot.notify_alert(alert_msg)
        else:
            logger.debug("All systems healthy ({} tasks monitored)", len(self._tasks))

    async def _check_maintenance_mode(self) -> None:
        """Detect exchange maintenance: 5+ WS disconnects in 10 min + REST failures."""
        if self._ws_disconnect_count >= 5:
            # Verify with REST call
            if self._exchange:
                try:
                    await self._exchange.fetch_balance()
                    # REST works, not maintenance - reset counter
                    self._ws_disconnect_count = 0
                    if self._maintenance_mode:
                        self._maintenance_mode = False
                        logger.info("Maintenance mode ended, exchange is back")
                        if self._telegram_bot:
                            await self._telegram_bot.send_message(
                                "\u2705 Exchange wieder erreichbar. Maintenance Mode beendet."
                            )
                except Exception:
                    if not self._maintenance_mode:
                        self._maintenance_mode = True
                        logger.warning("Maintenance mode activated: WS + REST both failing")
                        if self._telegram_bot:
                            await self._telegram_bot.notify_alert(
                                "Exchange Maintenance erkannt. "
                                "WebSocket und REST API nicht erreichbar. "
                                "Bot pausiert, Retry alle 5min."
                            )

    async def _check_clock_drift(self) -> None:
        """Check system clock is NTP synced (every 30 min)."""
        now = time.time()
        if now - getattr(self, "_last_clock_check", 0) < 1800:
            return
        self._last_clock_check = now

        try:
            proc = await asyncio.create_subprocess_exec(
                "timedatectl", "show", "--property=NTPSynchronized",
                stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
            )
            stdout, _ = await proc.communicate()
            output = stdout.decode().strip()
            if "NTPSynchronized=no" in output:
                logger.warning("System clock is NOT NTP synchronized!")
                if self._telegram_bot:
                    await self._telegram_bot.notify_alert(
                        "System-Uhr ist NICHT NTP-synchronisiert! "
                        "Zeitstempel koennten ungenau sein. "
                        "Fix: sudo timedatectl set-ntp true"
                    )
        except FileNotFoundError:
            pass  # timedatectl not available (e.g., Docker, Windows)
        except Exception as e:
            logger.debug("Clock drift check failed: {}", e)

    async def _check_db_backup(self) -> None:
        """Create daily SQLite backup (keep last 7)."""
        now = time.time()
        if now - self._last_backup < 86400:  # Once per day
            return
        self._last_backup = now

        try:
            from storage.database import DB_PATH
            if not DB_PATH.exists():
                return

            backup_dir = DB_PATH.parent / "backups"
            backup_dir.mkdir(exist_ok=True)

            timestamp = time.strftime("%Y%m%d_%H%M%S")
            backup_path = backup_dir / f"trading_bot_{timestamp}.db"
            shutil.copy2(str(DB_PATH), str(backup_path))
            logger.info("Database backed up to {}", backup_path)

            # Keep only last 7 backups
            backups = sorted(backup_dir.glob("trading_bot_*.db"), key=lambda p: p.stat().st_mtime)
            while len(backups) > 7:
                oldest = backups.pop(0)
                oldest.unlink()
                logger.debug("Removed old backup: {}", oldest.name)
        except Exception as e:
            logger.error("Database backup failed: {}", e)
