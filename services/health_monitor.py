from __future__ import annotations

import asyncio
import time
from utils.logger import logger


class HealthMonitor:
    """Watchdog that monitors all running tasks."""

    def __init__(self) -> None:
        self._tasks: dict[str, asyncio.Task] = {}
        self._last_heartbeat: dict[str, float] = {}
        self._running = False
        self._telegram_bot = None

    def set_telegram(self, telegram_bot) -> None:
        self._telegram_bot = telegram_bot

    def register_task(self, name: str, task: asyncio.Task) -> None:
        self._tasks[name] = task
        self._last_heartbeat[name] = time.time()

    def heartbeat(self, name: str) -> None:
        self._last_heartbeat[name] = time.time()

    async def start(self) -> None:
        self._running = True
        logger.info("Health monitor starting...")

        while self._running:
            await asyncio.sleep(60)
            if not self._running:
                break
            await self._check_health()

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
            if now - last > 300:
                issues.append(f"{name}: no heartbeat for {(now - last) / 60:.0f}min")

        if issues:
            alert_msg = "Health issues:\n" + "\n".join(f"- {i}" for i in issues)
            logger.warning(alert_msg)
            if self._telegram_bot:
                await self._telegram_bot.notify_alert(alert_msg)
        else:
            logger.debug("All systems healthy ({} tasks monitored)", len(self._tasks))
