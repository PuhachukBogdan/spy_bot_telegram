"""Payment-incident monitor: poll the feed, broadcast new, edit on update.

Tick logic (CLAUDE.md ops-alerts):
  1. Fetch + parse the feed (country/provider filtered; status kept).
  2. First run (empty table): record every current incident as ``seeded_only``
     WITHOUT broadcasting — so a fresh deploy never blasts history into groups.
     Counting starts from launch.
  3. Steady state, per incident:
     - unseen + active  → insert, broadcast to active groups, record messages.
     - unseen + resolved → skip (never announce a resolution we didn't post).
     - seen + seeded_only → update fields only; stays silent (no messages exist).
     - seen + posted    → update fields, edit the posted messages in place.

DB access never spans a network call (post-commit dispatch pattern): reads /
inserts in one short connection, the Telegram fan-out outside any connection,
then a short connection to persist message ids / edit outcomes.
"""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime

from aiogram import Bot

from src.config import settings
from src.db.client import acquire_connection
from src.db.queries.chats import list_active_group_chats
from src.pipeline.ops_alerts import state
from src.pipeline.ops_alerts.feed_parser import Incident, fetch_incidents
from src.pipeline.ops_alerts.templates import format_new_incident, format_update
from src.pipeline.ops_alerts.tg_sender import broadcast, edit_messages
from src.utils.logging import get_logger

log = get_logger(__name__)


def _stamp() -> str:
    return datetime.now(UTC).strftime("%Y-%m-%d %H:%M UTC")


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


async def _handle_new(bot: Bot, inc: Incident) -> None:
    """Insert + broadcast a never-seen active incident."""
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
        groups = await list_active_group_chats(conn)

    if not groups:
        log.info("ops.incidents.no_groups", incident_id=inc.incident_id)
        return

    targets = [(g.id, g.telegram_chat_id) for g in groups]
    text = format_new_incident(inc, detected_at=_stamp())
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


async def _handle_update(bot: Bot, inc: Incident, seeded_only: bool) -> None:
    """Update a known incident; edit its posted messages (skip if it was seeded)."""
    now = datetime.now(UTC)
    async with acquire_connection() as conn:
        await state.update_incident(
            conn,
            incident_id=inc.incident_id,
            status=inc.status,
            last_update=inc.iso_date or now,
            details=inc.details,
        )
        if seeded_only:
            return  # silent incident — nothing was posted, nothing to edit
        messages = await state.list_incident_messages(conn, inc.incident_id)

    if not messages:
        return

    text = format_update(inc, updated_at=_stamp())
    results = await edit_messages(
        bot, messages, text, concurrency=settings.OPS_BROADCAST_SEMAPHORE
    )

    async with acquire_connection() as conn:
        for r in results:
            if r.ok:
                await state.mark_message_edited(
                    conn, incident_id=inc.incident_id, chat_id=r.chat_id
                )
            else:
                await state.mark_message_edit_failed(
                    conn,
                    incident_id=inc.incident_id,
                    chat_id=r.chat_id,
                    reason=r.reason or "unknown",
                )
    log.info(
        "ops.incidents.updated",
        incident_id=inc.incident_id,
        edited=sum(1 for r in results if r.ok),
        failed=sum(1 for r in results if not r.ok),
    )


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
            await _handle_new(bot, inc)
        else:
            await _handle_update(bot, inc, seeded_only=bool(existing["seeded_only"]))


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
