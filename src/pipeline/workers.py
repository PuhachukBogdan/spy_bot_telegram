"""Background asyncio workers. Phase 4/9/10.

Phase 4 ships the abandoned-chat sweep: pending chats that were never authorized
within ``ABANDONED_CHAT_TIMEOUT_HOURS`` are left and marked ``'abandoned'``
(CLAUDE.md 7.2 "Cron каждый час"). The batch / priority / transcription workers
land in later phases and will register here too.

Workers are started as ``asyncio.Task``s from ``main.py``'s lifespan and cancelled
on shutdown.
"""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime, timedelta

from aiogram import Bot
from aiogram.exceptions import TelegramAPIError

from src.config import settings
from src.db.client import acquire_connection
from src.db.queries.audit import insert_audit_log
from src.db.queries.chats import list_stale_pending_chats, mark_chat_abandoned
from src.utils.logging import get_logger

log = get_logger(__name__)

# How often the abandoned-chat sweep runs (CLAUDE.md: hourly).
_CLEANUP_INTERVAL_SECONDS = 3600


async def run_abandoned_chat_sweep(bot: Bot) -> int:
    """Leave + mark pending chats older than the abandonment timeout. Returns count.

    The chat is left first (best-effort) and only then marked ``'abandoned'``,
    each in its own transaction with an audit row (system action: no actor). The
    ``mark_chat_abandoned`` guard makes this idempotent if the sweep overlaps a
    concurrent ``/authorize`` or a previous run.
    """
    cutoff = datetime.now(UTC) - timedelta(
        hours=settings.ABANDONED_CHAT_TIMEOUT_HOURS
    )
    async with acquire_connection() as conn:
        stale = await list_stale_pending_chats(conn, cutoff)

    swept = 0
    for chat in stale:
        await _leave_chat_quietly(bot, chat.telegram_chat_id)
        async with acquire_connection() as conn, conn.transaction():
            marked = await mark_chat_abandoned(conn, chat.telegram_chat_id)
            if marked is None:
                continue  # status changed under us; skip
            await insert_audit_log(
                conn,
                action="abandon_chat",
                target_entity="chat",
                target_id=marked.id,
                payload={
                    "telegram_chat_id": chat.telegram_chat_id,
                    "reason": "pending_timeout",
                    "timeout_hours": settings.ABANDONED_CHAT_TIMEOUT_HOURS,
                },
            )
            swept += 1
    return swept


async def abandoned_chat_cleanup_loop(
    bot: Bot, interval_seconds: int = _CLEANUP_INTERVAL_SECONDS
) -> None:
    """Run the abandoned-chat sweep forever, once per ``interval_seconds``.

    Sweeps immediately on start, then on the interval. Per-iteration errors are
    logged and swallowed so one bad run doesn't kill the loop; cancellation
    (shutdown) propagates.
    """
    log.info("worker.abandoned_cleanup.start", interval_s=interval_seconds)
    while True:
        try:
            swept = await run_abandoned_chat_sweep(bot)
            if swept:
                log.info("worker.abandoned_cleanup.swept", count=swept)
        except asyncio.CancelledError:
            log.info("worker.abandoned_cleanup.stop")
            raise
        except Exception as exc:  # never let one bad sweep kill the loop
            log.error("worker.abandoned_cleanup.error", error=str(exc))
        await asyncio.sleep(interval_seconds)


async def _leave_chat_quietly(bot: Bot, telegram_chat_id: int) -> None:
    """Leave a chat, swallowing API errors (it may already be gone)."""
    try:
        await bot.leave_chat(telegram_chat_id)
    except TelegramAPIError as exc:
        log.warning("worker.leave_failed", chat_id=telegram_chat_id, error=str(exc))
