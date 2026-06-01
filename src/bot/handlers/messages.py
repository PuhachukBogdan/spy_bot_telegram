"""Group-chat message + service-event handlers. Phase 5.

Registered for group / supergroup chats only. The whitelist middleware has
already dropped anything from a non-active chat, so handlers here can assume the
chat is an active partner chat (we still resolve its DB row to get the UUID).

Routing order inside the router matters: the service-event handlers
(member join/leave, title change, migration) are declared *before* the generic
content handler, so a service message is recorded as a ``chat_event`` and never
falls through to be ingested as a normal message.

The bot never replies here: every handler only reads Telegram and writes to the
DB (CLAUDE.md hard rule).
"""

from __future__ import annotations

from aiogram import F, Router
from aiogram.enums import ChatType
from aiogram.types import Message

from src.bot.topics import effective_topic_id
from src.db.client import acquire_connection
from src.db.queries.chat_events import insert_chat_event
from src.db.queries.chats import get_chat_unit, update_chat_telegram_id
from src.pipeline.ingest import ingest_message
from src.utils.logging import get_logger

log = get_logger(__name__)

router = Router(name="messages")
# Group-only: DM commands are handled by the dm_commands router; private messages
# are never ingested as monitored content.
router.message.filter(F.chat.type.in_({ChatType.GROUP, ChatType.SUPERGROUP}))


@router.message(F.migrate_to_chat_id)
async def on_migration(message: Message) -> None:
    """Group upgraded to supergroup: repoint the chat to its new id (CLAUDE.md 11.6).

    The service message arrives in the OLD chat (still active), carrying the new
    supergroup id. We record a ``migration`` event, then move the chat's
    ``telegram_chat_id`` so messages under the new id keep resolving.
    """
    new_id = message.migrate_to_chat_id
    if new_id is None:  # guarded by the filter; defensive for the type checker
        return
    old_id = message.chat.id
    async with acquire_connection() as conn:
        # Migration is group-level: record against the group-level unit, then
        # move every topic unit of the supergroup to the new id.
        chat = await get_chat_unit(conn, old_id, None)
        if chat is not None:
            await insert_chat_event(
                conn,
                chat_id=chat.id,
                event_type="migration",
                payload={"old_chat_id": old_id, "new_chat_id": new_id},
            )
        moved = await update_chat_telegram_id(conn, old_id, new_id)
    log.info("chat.migrated", old_chat_id=old_id, new_chat_id=new_id, units_moved=moved)


@router.message(F.new_chat_members)
async def on_members_joined(message: Message) -> None:
    """Record each member that joined the chat as a ``member_join`` event."""
    members = message.new_chat_members
    if not members:
        return
    actor = message.from_user
    async with acquire_connection() as conn:
        chat = await get_chat_unit(conn, message.chat.id, None)
        if chat is None:
            return
        for member in members:
            await insert_chat_event(
                conn,
                chat_id=chat.id,
                event_type="member_join",
                actor_user_id=actor.id if actor is not None else None,
                target_user_id=member.id,
                payload={"name": member.full_name, "is_bot": member.is_bot},
            )
    log.info("chat.members_joined", chat_id=message.chat.id, count=len(members))


@router.message(F.left_chat_member)
async def on_member_left(message: Message) -> None:
    """Record a member leaving / being removed as a ``member_leave`` event."""
    member = message.left_chat_member
    if member is None:
        return
    actor = message.from_user
    async with acquire_connection() as conn:
        chat = await get_chat_unit(conn, message.chat.id, None)
        if chat is None:
            return
        await insert_chat_event(
            conn,
            chat_id=chat.id,
            event_type="member_leave",
            actor_user_id=actor.id if actor is not None else None,
            target_user_id=member.id,
            payload={"name": member.full_name},
        )
    log.info("chat.member_left", chat_id=message.chat.id, member_id=member.id)


@router.message(F.new_chat_title)
async def on_title_change(message: Message) -> None:
    """Record a chat title change as a ``title_change`` event."""
    new_title = message.new_chat_title
    if new_title is None:
        return
    actor = message.from_user
    async with acquire_connection() as conn:
        chat = await get_chat_unit(conn, message.chat.id, None)
        if chat is None:
            return
        await insert_chat_event(
            conn,
            chat_id=chat.id,
            event_type="title_change",
            actor_user_id=actor.id if actor is not None else None,
            payload={"old_title": chat.chat_name, "new_title": new_title},
        )
    log.info("chat.title_changed", chat_id=message.chat.id)


@router.message()
async def on_content_message(message: Message) -> None:
    """Catch-all for real content: ingest it into the ``messages`` table.

    Reached only by non-service messages (the service handlers above match first).
    """
    thread_id = effective_topic_id(message)
    async with acquire_connection() as conn:
        chat = await get_chat_unit(conn, message.chat.id, thread_id)
    if chat is None:  # whitelist passed but row vanished; nothing to attach to
        log.warning("ingest.no_chat_row", chat_id=message.chat.id, thread_id=thread_id)
        return
    await ingest_message(message, chat)
