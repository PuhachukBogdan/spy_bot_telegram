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
from src.db.queries.chats import (
    count_live_units,
    list_stale_pending_chats,
    mark_chat_abandoned,
)
from src.db.queries.queue import claim_tasks
from src.pipeline.batch_processor import process_analysis_task
from src.pipeline.tier1 import pattern_cache
from src.pipeline.transcription import process_whisper_task
from src.utils.logging import get_logger

log = get_logger(__name__)

# How often the abandoned-chat sweep runs (CLAUDE.md: hourly).
_CLEANUP_INTERVAL_SECONDS = 3600
# How often the Tier-1 dictionary is hot-reloaded (CLAUDE.md Phase 6: ~5 min).
_PATTERN_RELOAD_INTERVAL_SECONDS = 300


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
        async with acquire_connection() as conn, conn.transaction():
            marked = await mark_chat_abandoned(
                conn, chat.telegram_chat_id, chat.message_thread_id
            )
            if marked is None:
                continue  # status changed under us; skip
            await insert_audit_log(
                conn,
                action="abandon_chat",
                target_entity="chat",
                target_id=marked.id,
                payload={
                    "telegram_chat_id": chat.telegram_chat_id,
                    "thread_id": chat.message_thread_id,
                    "reason": "pending_timeout",
                    "timeout_hours": settings.ABANDONED_CHAT_TIMEOUT_HOURS,
                },
            )
            # Leave the whole supergroup only once no monitored unit of it remains
            # (leaving kills every topic); abandon the unit first, then check.
            remaining = await count_live_units(conn, chat.telegram_chat_id)
        if remaining == 0:
            await _leave_chat_quietly(bot, chat.telegram_chat_id)
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


async def whisper_worker_loop(
    bot: Bot,
    interval_seconds: int | None = None,
    batch_size: int | None = None,
) -> None:
    """Consume ``whisper_transcribe`` tasks forever (CLAUDE.md Phase 7).

    Each tick claims a small batch with ``FOR UPDATE SKIP LOCKED`` and processes
    them sequentially; each task self-resolves to done/failed/retry inside
    ``process_whisper_task``. The claim runs in its own short connection so the
    slow download + API work never holds a row lock. Always runs (even with
    ``WHISPER_ENABLED=false``) so the queue drains rather than backs up.
    Per-iteration errors are logged and swallowed; cancellation propagates.
    """
    interval = interval_seconds or settings.WHISPER_POLL_INTERVAL_SECONDS
    batch = batch_size or settings.WHISPER_BATCH_SIZE
    log.info(
        "worker.whisper.start",
        interval_s=interval,
        batch=batch,
        enabled=settings.WHISPER_ENABLED,
    )
    while True:
        try:
            async with acquire_connection() as conn:
                tasks = await claim_tasks(conn, "whisper_transcribe", batch)
            for task in tasks:
                await process_whisper_task(bot, task)
        except asyncio.CancelledError:
            log.info("worker.whisper.stop")
            raise
        except Exception as exc:  # never let one bad tick kill the loop
            log.error("worker.whisper.error", error=str(exc))
        await asyncio.sleep(interval)


async def analysis_worker_loop(
    bot: Bot,
    interval_seconds: int | None = None,
    batch_size: int | None = None,
) -> None:
    """Consume ``analyze_chat`` tasks forever (CLAUDE.md Phase 9, unified lane).

    Each tick claims a small batch of due tasks (``FOR UPDATE SKIP LOCKED``, ordered
    by ``scheduled_for`` so bumped/priority chats go first) and processes them
    sequentially; each task self-resolves to done/failed/retry inside
    ``process_analysis_task``. The claim runs in its own short connection so the
    slow LLM call never holds a row lock. ``bot`` is threaded through for the
    Phase-11 alert path's emergency Telegram fallback (Slack down). Per-iteration
    errors are logged and swallowed; cancellation propagates.
    """
    interval = interval_seconds or settings.ANALYSIS_POLL_INTERVAL_SECONDS
    batch = batch_size or settings.ANALYSIS_BATCH_SIZE
    log.info("worker.analysis.start", interval_s=interval, batch=batch)
    while True:
        try:
            async with acquire_connection() as conn:
                tasks = await claim_tasks(conn, "analyze_chat", batch)
            for task in tasks:
                await process_analysis_task(bot, task)
        except asyncio.CancelledError:
            log.info("worker.analysis.stop")
            raise
        except Exception as exc:  # never let one bad tick kill the loop
            log.error("worker.analysis.error", error=str(exc))
        await asyncio.sleep(interval)


async def pattern_reload_loop(
    interval_seconds: int = _PATTERN_RELOAD_INTERVAL_SECONDS,
) -> None:
    """Hot-reload the Tier-1 dictionary on the interval (CLAUDE.md Phase 6).

    The cache only recompiles when its fingerprint changes, so most ticks are a
    single cheap aggregate query. Errors are logged and swallowed; cancellation
    propagates.
    """
    log.info("worker.pattern_reload.start", interval_s=interval_seconds)
    while True:
        try:
            async with acquire_connection() as conn:
                reloaded = await pattern_cache.refresh(conn)
            if reloaded:
                log.info("worker.pattern_reload.refreshed", count=pattern_cache.size)
        except asyncio.CancelledError:
            log.info("worker.pattern_reload.stop")
            raise
        except Exception as exc:  # never let one bad reload kill the loop
            log.error("worker.pattern_reload.error", error=str(exc))
        await asyncio.sleep(interval_seconds)
