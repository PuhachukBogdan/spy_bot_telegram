"""Payment-incident monitor: poll the feed, broadcast a problem, then a recovery.

Tick logic (CLAUDE.md ops-alerts):
  1. Fetch + parse the feed (country/provider filtered; status kept).
  2. First run (empty table): record every current incident as ``seeded_only``
     WITHOUT broadcasting — so a fresh deploy never blasts history into groups.
     Counting starts from launch.
  3. Steady state, per incident:
     - unseen + active  → insert as PENDING (no broadcast yet). We hold the alert
       for ``OPS_INCIDENT_BROADCAST_DELAY_SECONDS`` so a provider that recovers
       inside the window never reaches partners.
     - unseen + resolved → skip (never announce a resolution we didn't post).
     - seen + seeded_only → update fields only; stays silent (no messages exist).
     - seen + pending (no messages yet):
         · resolved inside the window → update silently, never announce it.
         · still active past the delay → broadcast the PSP alert + record messages.
         · still active inside the delay → keep waiting.
     - seen + posted (a PSP alert went out):
         · still active → nothing to do (the alert already stands; the minimal
           template carries no status to refresh, so we never edit in place).
         · resolved, recovery not yet posted → broadcast a one-shot recovery
           alert into the same groups + flag ``recovery_posted``.
         · resolved, recovery already posted → nothing to do.

DB access never spans a network call (post-commit dispatch pattern): reads /
inserts in one short connection, the Telegram fan-out outside any connection,
then a short connection to persist message ids / the recovery flag.
"""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime
from typing import Any

from aiogram import Bot

from src.config import settings
from src.db.client import acquire_connection
from src.db.queries.chats import list_active_group_chats
from src.pipeline.ops_alerts import state
from src.pipeline.ops_alerts.feed_parser import Incident, fetch_incidents
from src.pipeline.ops_alerts.templates import format_new_incident, format_recovery
from src.pipeline.ops_alerts.tg_sender import broadcast
from src.utils.logging import get_logger

log = get_logger(__name__)


def _stamp() -> str:
    return datetime.now(UTC).strftime("%Y-%m-%d %H:%M UTC")


def _fmt(dt: datetime | None) -> str:
    """Format the incident's feed timestamp for the 'Last update' line."""
    return dt.strftime("%Y-%m-%d %H:%M UTC") if dt else _stamp()


async def _seed_first_run(incidents: list[Incident]) -> None:
    """Record all current incidents as seen, silently (no broadcast)."""
    now = datetime.now(UTC)
    async with acquire_connection() as conn:
        for inc in incidents:
            await state.insert_incident(
                conn,
                incident_id=inc.incident_id,
                country=inc.country,
                provider=inc.provider,
                issue=inc.issue,
                link=inc.link,
                details=inc.details,
                status=inc.status,
                iso_date=inc.iso_date,
                last_update=inc.iso_date or now,
                seeded_only=True,
            )
    log.info("ops.incidents.seeded", count=len(incidents))


async def _record_pending(inc: Incident) -> None:
    """First sight of an active incident: record it, do NOT broadcast yet.

    The broadcast is deferred by ``OPS_INCIDENT_BROADCAST_DELAY_SECONDS``. A later
    tick promotes it (``_broadcast_pending``) once it is still active past the
    delay; if it resolves inside the window it is never announced. The row's
    ``created_at`` (DB default now()) is our first-detection clock.
    """
    now = datetime.now(UTC)
    async with acquire_connection() as conn:
        await state.insert_incident(
            conn,
            incident_id=inc.incident_id,
            country=inc.country,
            provider=inc.provider,
            issue=inc.issue,
            link=inc.link,
            details=inc.details,
            status=inc.status,
            iso_date=inc.iso_date,
            last_update=inc.iso_date or now,
            seeded_only=False,
        )
    log.info("ops.incidents.pending", incident_id=inc.incident_id)


async def _broadcast_pending(bot: Bot, inc: Incident) -> None:
    """Promote a pending incident: broadcast it now and record the sent messages."""
    async with acquire_connection() as conn:
        groups = await list_active_group_chats(conn)

    if not groups:
        log.info("ops.incidents.no_groups", incident_id=inc.incident_id)
        return

    targets = [(g.id, g.telegram_chat_id) for g in groups]
    text = format_new_incident(inc, last_update=_fmt(inc.iso_date))
    sent = await broadcast(
        bot, targets, text, concurrency=settings.OPS_BROADCAST_SEMAPHORE
    )

    async with acquire_connection() as conn:
        for s in sent:
            await state.record_message(
                conn,
                incident_id=inc.incident_id,
                chat_id=s.chat_id,
                telegram_message_id=s.telegram_message_id,
            )
    log.info(
        "ops.incidents.broadcast",
        incident_id=inc.incident_id,
        groups=len(targets),
        delivered=len(sent),
    )


async def _broadcast_recovery(
    bot: Bot, inc: Incident, messages: list[dict[str, Any]]
) -> None:
    """A previously-announced incident recovered: broadcast the recovery, once.

    Fans out to the exact chats that received the original PSP alert (from
    ``payment_incident_messages``), so the recovery reaches precisely the groups
    that saw the problem. The recovery is a fresh one-shot message — it is never
    edited and never recorded, and ``recovery_posted`` guards against a re-send on
    the next tick.
    """
    targets = [(m["chat_id"], m["telegram_chat_id"]) for m in messages]
    text = format_recovery(inc, last_update=_fmt(inc.iso_date))
    sent = await broadcast(
        bot, targets, text, concurrency=settings.OPS_BROADCAST_SEMAPHORE
    )

    async with acquire_connection() as conn:
        await state.mark_recovery_posted(conn, inc.incident_id)
    log.info(
        "ops.incidents.recovery",
        incident_id=inc.incident_id,
        groups=len(targets),
        delivered=len(sent),
    )


async def _handle_update(bot: Bot, inc: Incident, existing: dict[str, Any]) -> None:
    """Refresh a known incident, then act on its stage.

    Stages after the field update:
      - seeded_only        → silent (nothing was ever posted).
      - pending (no msgs)  → recovered inside the delay → stay silent; still active
        past the delay → broadcast the PSP alert; still active inside it → wait.
      - already broadcast  → still active → nothing (no in-place edits); resolved →
        broadcast a one-shot recovery alert unless it already went out.
    """
    now = datetime.now(UTC)
    async with acquire_connection() as conn:
        await state.update_incident(
            conn,
            incident_id=inc.incident_id,
            status=inc.status,
            last_update=inc.iso_date or now,
            details=inc.details,
        )
        if bool(existing["seeded_only"]):
            return  # silent incident — nothing was posted, nothing to announce
        messages = await state.list_incident_messages(conn, inc.incident_id)

    if not messages:
        # Pending: recorded on first sight but held for the broadcast delay.
        if inc.is_resolved:
            log.info("ops.incidents.recovered_in_window", incident_id=inc.incident_id)
            return  # recovered before the delay elapsed — never announce it
        age = (now - existing["created_at"]).total_seconds()
        if age >= settings.OPS_INCIDENT_BROADCAST_DELAY_SECONDS:
            await _broadcast_pending(bot, inc)
        else:
            log.info(
                "ops.incidents.pending_wait",
                incident_id=inc.incident_id,
                age_seconds=int(age),
            )
        return

    # The PSP alert already went out. The minimal template carries no status to
    # refresh, so a still-active incident needs nothing; only a recovery speaks.
    if not inc.is_resolved:
        return
    if bool(existing["recovery_posted"]):
        return  # recovery already broadcast — never re-send it
    await _broadcast_recovery(bot, inc, messages)


async def run_incidents_tick(bot: Bot) -> None:
    """One poll cycle. Raises only on unrecoverable fetch failure (loop catches)."""
    feed = settings.OPS_FEED_URL
    if feed is None:
        return  # incidents branch disabled (no feed configured)

    incidents = await fetch_incidents(
        feed.get_secret_value(),
        retries=settings.OPS_FEED_HTTP_RETRIES,
        retry_delay_seconds=settings.OPS_FEED_HTTP_RETRY_DELAY_SECONDS,
    )

    async with acquire_connection() as conn:
        first_run = await state.count_incidents(conn) == 0

    if first_run:
        if incidents:
            await _seed_first_run(incidents)
        return

    for inc in incidents:
        async with acquire_connection() as conn:
            existing = await state.get_incident(conn, inc.incident_id)
        if existing is None:
            if inc.is_resolved:
                continue  # never announce a resolution we didn't post
            await _record_pending(inc)
        else:
            await _handle_update(bot, inc, existing)


async def incidents_worker_loop(bot: Bot, interval_seconds: int | None = None) -> None:
    """Poll the payment feed forever. Errors logged + swallowed; cancel propagates."""
    interval = interval_seconds or settings.OPS_INCIDENTS_POLL_INTERVAL_SECONDS
    log.info("worker.ops_incidents.start", interval_s=interval)
    while True:
        try:
            await run_incidents_tick(bot)
        except asyncio.CancelledError:
            log.info("worker.ops_incidents.stop")
            raise
        except Exception as exc:  # never let one bad tick kill the loop
            log.error("worker.ops_incidents.error", error=str(exc))
        await asyncio.sleep(interval)
