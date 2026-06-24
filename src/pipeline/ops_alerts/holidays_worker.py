"""Argentina holiday reminder: post a heads-up the day before, once per holiday.

Follows the in-process scheduler pattern (like ``summary_scheduler_loop``): a
plain asyncio loop, no APScheduler. Each tick checks whether local time in
``OPS_HOLIDAYS_TIMEZONE`` has passed the configured hour and tomorrow is a
holiday; the send is claimed atomically in ``ops_holiday_sends`` so it fires
exactly once per holiday across restarts and overlapping ticks.
"""

from __future__ import annotations

import asyncio
from datetime import datetime
from zoneinfo import ZoneInfo

from aiogram import Bot

from src.config import settings
from src.db.client import acquire_connection
from src.db.queries.chats import list_active_group_chats
from src.pipeline.ops_alerts import state
from src.pipeline.ops_alerts.holidays_calendar import find_tomorrow_holiday
from src.pipeline.ops_alerts.templates import format_holiday
from src.pipeline.ops_alerts.tg_sender import broadcast
from src.utils.logging import get_logger

log = get_logger(__name__)


async def run_holidays_tick(bot: Bot) -> bool:
    """One check. Returns True if a reminder was broadcast this tick.

    Gates on: local hour ≥ configured hour, tomorrow is a holiday, and the send
    hasn't been claimed yet. Claiming before the broadcast prevents a double post
    if two ticks overlap; a one-shot reminder is never edited or re-sent.
    """
    tz = ZoneInfo(settings.OPS_HOLIDAYS_TIMEZONE)
    now_local = datetime.now(tz)
    if now_local.hour < settings.OPS_HOLIDAYS_CRON_HOUR:
        return False  # too early in the day — wait for the configured hour

    holiday = find_tomorrow_holiday(now_local.date())
    if holiday is None:
        return False

    async with acquire_connection() as conn:
        claimed = await state.record_holiday_sent(conn, holiday.date, holiday.name)
        if not claimed:
            return False  # already reminded about this holiday
        groups = await list_active_group_chats(conn)

    if not groups:
        log.info("ops.holidays.no_groups", holiday=holiday.name)
        return True

    targets = [(g.id, g.telegram_chat_id) for g in groups]
    sent = await broadcast(
        bot, targets, format_holiday(holiday),
        concurrency=settings.OPS_BROADCAST_SEMAPHORE,
    )
    log.info(
        "ops.holidays.broadcast",
        holiday=holiday.name,
        date=holiday.date.isoformat(),
        groups=len(targets),
        delivered=len(sent),
    )
    return True


async def holidays_worker_loop(bot: Bot, interval_seconds: int | None = None) -> None:
    """Check for an upcoming holiday forever. Errors logged + swallowed."""
    interval = interval_seconds or settings.OPS_HOLIDAYS_POLL_INTERVAL_SECONDS
    log.info(
        "worker.ops_holidays.start",
        interval_s=interval,
        tz=settings.OPS_HOLIDAYS_TIMEZONE,
        hour=settings.OPS_HOLIDAYS_CRON_HOUR,
    )
    while True:
        try:
            await run_holidays_tick(bot)
        except asyncio.CancelledError:
            log.info("worker.ops_holidays.stop")
            raise
        except Exception as exc:  # never let one bad tick kill the loop
            log.error("worker.ops_holidays.error", error=str(exc))
        await asyncio.sleep(interval)
