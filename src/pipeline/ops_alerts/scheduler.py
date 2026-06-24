"""Start/stop the ops-alerts workers as asyncio tasks.

Called from ``main.py``'s lifespan alongside the other workers. Gated by
``OPS_ALERTS_ENABLED``: when off, no tasks are started (returns an empty list).
The incidents branch additionally self-skips when ``OPS_FEED_URL`` is unset.
"""

from __future__ import annotations

import asyncio
import contextlib

from aiogram import Bot

from src.config import settings
from src.pipeline.ops_alerts.holidays_worker import holidays_worker_loop
from src.pipeline.ops_alerts.incidents_worker import incidents_worker_loop
from src.utils.logging import get_logger

log = get_logger(__name__)


def start_ops_alerts(bot: Bot) -> list[asyncio.Task[None]]:
    """Spawn the ops-alerts worker tasks. Empty list if the subsystem is off."""
    if not settings.OPS_ALERTS_ENABLED:
        log.info("ops_alerts.disabled")
        return []

    tasks = [
        asyncio.create_task(incidents_worker_loop(bot), name="ops_incidents_worker"),
        asyncio.create_task(holidays_worker_loop(bot), name="ops_holidays_worker"),
    ]
    log.info("ops_alerts.started", workers=len(tasks))
    return tasks


async def stop_ops_alerts(tasks: list[asyncio.Task[None]]) -> None:
    """Cancel + await the ops-alerts tasks on shutdown."""
    for task in tasks:
        task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await task
