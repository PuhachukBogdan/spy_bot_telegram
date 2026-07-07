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
from decimal import Decimal

from aiogram import Bot
from aiogram.exceptions import TelegramAPIError

from src.alerts.slack import SlackDeliveryError, build_alert_blocks, post_alert
from src.alerts.system import send_budget_exceeded_alert
from src.config import settings
from src.db.client import acquire_connection
from src.db.models import FailedAlert
from src.db.queries.alerts import (
    bump_failed_alert_attempt,
    list_unresolved_failed_alerts,
    mark_failed_alert_resolved,
)
from src.db.queries.audit import insert_audit_log
from src.db.queries.chats import (
    count_live_units,
    get_chat_by_id,
    list_stale_pending_chats,
    mark_chat_abandoned,
)
from src.db.queries.cost import get_today, is_circuit_open, trip_circuit_breaker
from src.db.queries.messages import get_message_timestamp
from src.db.queries.partners import get_partner_by_id
from src.db.queries.queue import claim_tasks, recover_stale_tasks
from src.db.queries.risk_events import get_by_ref, set_slack_message_ts
from src.db.queries.summaries import summary_exists_since
from src.pipeline.batch_processor import process_analysis_task
from src.pipeline.file_processor import process_file_task
from src.pipeline.tier1 import pattern_cache
from src.pipeline.transcription import process_whisper_task
from src.summary.generator import generate_report
from src.utils.logging import get_logger

log = get_logger(__name__)

# How often the abandoned-chat sweep runs (CLAUDE.md: hourly).
_CLEANUP_INTERVAL_SECONDS = 3600
# How often the Tier-1 dictionary is hot-reloaded (CLAUDE.md Phase 6: ~5 min).
_PATTERN_RELOAD_INTERVAL_SECONDS = 300

# --- Summary scheduler (replaces the old n8n cron) --------------------------
# How often the scheduler wakes to check whether a report is due (15 min). The
# DB dedup makes the exact tick granularity irrelevant — a report fires within
# one interval of its scheduled instant.
_SUMMARY_SCHEDULER_INTERVAL_SECONDS = 900
# Catch-up window: fire a due slot only if its scheduled instant is at most this
# old. Covers a short outage spanning the slot, without blasting a stale report
# on a fresh deploy days later.
_SUMMARY_CATCHUP_WINDOW_SECONDS = 21600  # 6h
# Weekly: Monday 08:00 UTC (n8n cron "0 8 * * 1").
_WEEKLY_DOW = 0  # Monday (datetime.weekday(): Monday == 0)
_WEEKLY_HOUR = 8
# Monthly: 1st of month 08:00 UTC (n8n cron "0 8 1 * *").
_MONTHLY_HOUR = 8


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


async def _budget_gate(bot: Bot, worker_name: str) -> bool:
    """Return True if the daily LLM budget is exhausted (worker must skip this tick).

    Side effect when newly tripped: calls ``trip_circuit_breaker`` then fires
    ``send_budget_exceeded_alert`` (best-effort). Never raises — a gate error
    returns False so workers are not blocked by monitoring failure.
    """
    try:
        async with acquire_connection() as conn:
            if await is_circuit_open(conn):
                log.debug("worker.budget_gate.open", worker=worker_name)
                return True
            today = await get_today(conn)
        spend: Decimal = (today.total_cost_usd or Decimal("0")) if today else Decimal("0")
        if spend >= settings.DAILY_LLM_BUDGET_USD:
            async with acquire_connection() as conn:
                newly_tripped = await trip_circuit_breaker(conn)
            if newly_tripped:
                log.warning(
                    "worker.budget_gate.tripped",
                    spend=str(spend),
                    limit=str(settings.DAILY_LLM_BUDGET_USD),
                )
                await send_budget_exceeded_alert(
                    bot, spend, settings.DAILY_LLM_BUDGET_USD
                )
            return True
        return False
    except Exception as exc:
        log.error("worker.budget_gate.error", worker=worker_name, error=str(exc))
        return False


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
            if await _budget_gate(bot, "analysis"):
                await asyncio.sleep(interval)
                continue
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


async def file_analysis_worker_loop(
    bot: Bot,
    interval_seconds: int | None = None,
    batch_size: int | None = None,
) -> None:
    """Consume ``analyze_file`` tasks forever.

    Each tick claims a small batch and processes them sequentially. Runs even
    when ``FILE_ANALYSIS_ENABLED=false`` so the queue drains. Per-iteration
    errors are logged and swallowed; cancellation propagates.
    """
    interval = interval_seconds or settings.FILE_ANALYSIS_POLL_INTERVAL_SECONDS
    batch = batch_size or settings.FILE_ANALYSIS_BATCH_SIZE
    log.info(
        "worker.file_analysis.start",
        interval_s=interval,
        batch=batch,
        enabled=settings.FILE_ANALYSIS_ENABLED,
    )
    while True:
        try:
            if await _budget_gate(bot, "file_analysis"):
                await asyncio.sleep(interval)
                continue
            async with acquire_connection() as conn:
                tasks = await claim_tasks(conn, "analyze_file", batch)
            for task in tasks:
                await process_file_task(bot, task)
        except asyncio.CancelledError:
            log.info("worker.file_analysis.stop")
            raise
        except Exception as exc:
            log.error("worker.file_analysis.error", error=str(exc))
        await asyncio.sleep(interval)


async def stale_task_reaper_loop(
    interval_seconds: int | None = None,
) -> None:
    """Reset tasks orphaned in ``in_progress`` due to worker crashes / restarts.

    Runs every ``STALE_TASK_REAPER_INTERVAL_SECONDS``. Any task that has been
    ``in_progress`` for longer than ``STALE_TASK_TIMEOUT_SECONDS`` is either
    reset to ``pending`` (retry budget remaining) or permanently ``failed``
    (attempts exhausted). Logs a warning whenever tasks are recovered so ops
    can correlate with deploy events.
    """
    interval = interval_seconds or settings.STALE_TASK_REAPER_INTERVAL_SECONDS
    log.info("worker.stale_reaper.start", interval_s=interval)
    while True:
        try:
            stale_before = datetime.now(UTC) - timedelta(
                seconds=settings.STALE_TASK_TIMEOUT_SECONDS
            )
            async with acquire_connection() as conn:
                recovered = await recover_stale_tasks(
                    conn,
                    stale_before=stale_before,
                    max_attempts=settings.ANALYSIS_MAX_ATTEMPTS,
                )
            if recovered:
                log.warning(
                    "worker.stale_reaper.recovered",
                    count=recovered,
                    timeout_s=settings.STALE_TASK_TIMEOUT_SECONDS,
                )
        except asyncio.CancelledError:
            log.info("worker.stale_reaper.stop")
            raise
        except Exception as exc:
            log.error("worker.stale_reaper.error", error=str(exc))
        await asyncio.sleep(interval)


async def _retry_one_failed_alert(bot: Bot, alert: FailedAlert) -> None:
    """Re-post one undelivered alert, rebuilt from the live risk_event.

    The card is re-rendered from the current risk_event (source of truth), never a
    stale stored payload. On success the event gets its slack ts and the breadcrumb
    is resolved; on a repeat Slack failure the attempt count is bumped (and the row
    retried next tick until the cap). An orphaned/already-delivered row is resolved
    without re-posting. Never raises — a bad row must not stall the loop.
    """
    if alert.risk_event_id is None:
        async with acquire_connection() as conn:
            await mark_failed_alert_resolved(conn, alert.id)
        return

    async with acquire_connection() as conn:
        event = await get_by_ref(conn, str(alert.risk_event_id))
    # Gone, or already delivered by some other path → nothing to retry.
    if event is None or event.slack_message_ts is not None or event.chat_id is None:
        async with acquire_connection() as conn:
            await mark_failed_alert_resolved(conn, alert.id)
        return

    async with acquire_connection() as conn:
        chat = await get_chat_by_id(conn, event.chat_id)
        if chat is None:
            await mark_failed_alert_resolved(conn, alert.id)
            return
        partner = (
            await get_partner_by_id(conn, event.partner_id)
            if event.partner_id is not None
            else None
        )
        message_dt = (
            await get_message_timestamp(conn, event.message_id)
            if event.message_id
            else None
        )
    partner_name = partner.name if partner is not None else None

    blocks, text = build_alert_blocks(
        event, chat, partner_name, message_dt=message_dt
    )
    try:
        ts = await post_alert(channel=alert.channel, text=text, blocks=blocks)
    except SlackDeliveryError as exc:
        async with acquire_connection() as conn:
            await bump_failed_alert_attempt(conn, alert.id)
        log.warning(
            "worker.failed_alert_retry.still_failing",
            failed_alert_id=str(alert.id),
            error=str(exc),
        )
        return

    async with acquire_connection() as conn:
        await set_slack_message_ts(conn, event.id, ts)
        await mark_failed_alert_resolved(conn, alert.id)
    log.info(
        "worker.failed_alert_retry.delivered",
        failed_alert_id=str(alert.id),
        risk_event_id=str(event.id)[:8],
    )


async def failed_alert_retry_loop(
    bot: Bot, interval_seconds: int | None = None
) -> None:
    """Periodically re-deliver undelivered Slack alerts once Slack recovers.

    Reads the ``failed_alerts`` breadcrumbs (Phase 11) and retries each, up to
    ``FAILED_ALERT_MAX_RETRIES``. Each retry is isolated; a single bad row is
    logged and skipped, never aborting the tick or the loop.
    """
    interval = interval_seconds or settings.FAILED_ALERT_RETRY_INTERVAL_SECONDS
    log.info("worker.failed_alert_retry.start", interval_s=interval)
    while True:
        try:
            async with acquire_connection() as conn:
                pending = await list_unresolved_failed_alerts(
                    conn, max_retries=settings.FAILED_ALERT_MAX_RETRIES
                )
            if pending:
                log.info("worker.failed_alert_retry.batch", count=len(pending))
                for alert in pending:
                    try:
                        await _retry_one_failed_alert(bot, alert)
                    except Exception as exc:  # isolate one bad row
                        log.error(
                            "worker.failed_alert_retry.row_error",
                            failed_alert_id=str(alert.id),
                            error=str(exc),
                        )
        except asyncio.CancelledError:
            log.info("worker.failed_alert_retry.stop")
            raise
        except Exception as exc:
            log.error("worker.failed_alert_retry.error", error=str(exc))
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


def _last_weekly_occurrence(now: datetime) -> datetime:
    """Most recent Monday 08:00 UTC at or before ``now``."""
    occ = now.replace(hour=_WEEKLY_HOUR, minute=0, second=0, microsecond=0)
    occ -= timedelta(days=(now.weekday() - _WEEKLY_DOW) % 7)
    if occ > now:  # earlier today than 08:00 on this week's Monday → last week
        occ -= timedelta(days=7)
    return occ


def _last_monthly_occurrence(now: datetime) -> datetime:
    """Most recent 1st-of-month 08:00 UTC at or before ``now``."""
    occ = now.replace(day=1, hour=_MONTHLY_HOUR, minute=0, second=0, microsecond=0)
    if occ > now:  # before the 1st @ 08:00 → step into previous month
        occ = (occ - timedelta(days=1)).replace(
            day=1, hour=_MONTHLY_HOUR, minute=0, second=0, microsecond=0
        )
    return occ


async def run_summary_scheduler_tick() -> list[str]:
    """One scheduler pass: fire any due, not-yet-generated report.

    Returns the period types fired this tick (for tests/observability). A slot
    fires only if its scheduled instant is within the catch-up window AND no
    summary of that type was generated since that instant (restart-safe dedup).
    """
    now = datetime.now(UTC)
    schedule = (
        ("weekly", _last_weekly_occurrence(now)),
        ("monthly", _last_monthly_occurrence(now)),
    )
    fired: list[str] = []
    for period_type, occ in schedule:
        if (now - occ).total_seconds() > _SUMMARY_CATCHUP_WINDOW_SECONDS:
            continue  # missed slot too old — wait for the next occurrence
        async with acquire_connection() as conn:
            if await summary_exists_since(conn, period_type, occ):
                continue  # already handled this slot
        log.info(
            "worker.summary_scheduler.fire",
            period_type=period_type,
            scheduled_for=occ.isoformat(),
        )
        result = await generate_report(period_type=period_type)  # type: ignore[arg-type]
        fired.append(period_type)
        log.info(
            "worker.summary_scheduler.done",
            period_type=period_type,
            url=result.url,
            event_count=result.event_count,
            slack_delivered=result.slack_delivered,
        )
    return fired


async def summary_scheduler_loop(
    interval_seconds: int = _SUMMARY_SCHEDULER_INTERVAL_SECONDS,
) -> None:
    """Fire weekly/monthly summary reports on schedule — replaces the n8n cron.

    Weekly: Monday 08:00 UTC. Monthly: 1st of month 08:00 UTC. Checks every
    ``interval_seconds``; the per-slot DB dedup (``summary_exists_since``) makes
    firing idempotent across restarts and overlapping ticks. Per-iteration errors
    are logged and swallowed so one bad run doesn't kill the loop; cancellation
    propagates.
    """
    log.info("worker.summary_scheduler.start", interval_s=interval_seconds)
    while True:
        try:
            await run_summary_scheduler_tick()
        except asyncio.CancelledError:
            log.info("worker.summary_scheduler.stop")
            raise
        except Exception as exc:  # never let one bad tick kill the loop
            log.error("worker.summary_scheduler.error", error=str(exc))
        await asyncio.sleep(interval_seconds)
