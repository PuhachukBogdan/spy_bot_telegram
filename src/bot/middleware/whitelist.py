"""Ignore messages from non-active units; discover new forum topics. Phase 3+.

Registered as an *outer* middleware on the message observer so it runs for every
incoming message before routing. A monitored unit is ``(telegram_chat_id,
topic)`` (see ``bot/topics.py``). Policy:

  - Private chats (DMs with staff) always pass — that's how commands work.
  - Group / supergroup messages pass only when the unit's ``status`` is
    ``'active'`` in the DB. Pending / banned / abandoned units are dropped.
  - An *unknown* forum-topic unit is discovered: a pending row is created and
    admins are DM'd once (Telegram sends no per-topic add event, so topics can
    only be found from their first message). The message itself is still dropped
    — nothing is stored until the topic is authorized.

Onboarding events (``my_chat_member`` / ``chat_member``) are *not* messages, so
they bypass this middleware; the group-level unit is created there.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from typing import Any

from aiogram import BaseMiddleware, Bot
from aiogram.enums import ChatType
from aiogram.types import Message, TelegramObject

from src.bot.notify import notify_admins_pending
from src.bot.topics import effective_topic_id
from src.db.client import acquire_connection
from src.db.queries.chats import create_pending_chat, get_by_unit
from src.db.queries.etc import list_admin_users
from src.utils.logging import get_logger

log = get_logger(__name__)


class WhitelistMiddleware(BaseMiddleware):
    """Drop messages from units that are not active; discover new forum topics."""

    async def __call__(
        self,
        handler: Callable[[TelegramObject, dict[str, Any]], Awaitable[Any]],
        event: TelegramObject,
        data: dict[str, Any],
    ) -> Any:
        if not isinstance(event, Message):
            return await handler(event, data)

        chat = event.chat
        if chat.type == ChatType.PRIVATE:
            return await handler(event, data)

        # topic_key: the forum topic id for a real topic message, else 0 (whole
        # group / General / non-forum). effective_topic_id guards is_forum AND
        # is_topic_message, so plain reply-threads do NOT become separate units.
        topic_key = effective_topic_id(event) or 0
        async with acquire_connection() as conn:
            unit = await get_by_unit(conn, chat.id, topic_key)

        if unit is not None and unit.status == "active":
            return await handler(event, data)

        # Unknown unit AND it's a topic (topic_key != 0): try to discover it so an
        # admin can authorize. Group-level units (topic_key 0) are created by the
        # onboarding handler, not here.
        if unit is None and topic_key != 0:
            await self._maybe_discover_topic(event, topic_key, data)

        # Not active: ignore. Debug level — the common steady state for any unit
        # we haven't authorized, not an error.
        log.debug(
            "whitelist.skip",
            chat_id=chat.id,
            topic_key=topic_key,
            status=unit.status if unit is not None else "unknown",
        )
        return None

    async def _maybe_discover_topic(
        self, message: Message, thread_id: int, data: dict[str, Any]
    ) -> None:
        """Register a newly-seen forum topic as pending and DM admins once.

        Only fires when the supergroup's group-level parent unit is already
        ``'active'`` — we don't surface topics of a group we were never authorized
        to watch. Idempotent via ``create_pending_chat``'s ON CONFLICT: only the
        first message in a topic creates the row and notifies; later ones find a
        ``'pending'`` unit and never reach here again.
        """
        chat = message.chat
        if chat.type != ChatType.SUPERGROUP or not message.is_topic_message:
            return

        async with acquire_connection() as conn:
            parent = await get_by_unit(conn, chat.id, 0)
            if parent is None or parent.status != "active":
                return  # parent group not authorized → don't discover its topics
            created = await create_pending_chat(
                conn,
                telegram_chat_id=chat.id,
                thread_id=thread_id,
                chat_name=chat.title,
                added_by_user_id=None,  # we don't know who created the topic
                unit_type="topic",
            )
            if created is None:  # lost a race; already discovered
                return
            admins = await list_admin_users(conn)

        log.info(
            "onboarding.topic_discovered",
            chat_id=chat.id,
            thread_id=thread_id,
            parent=parent.chat_name,
        )
        bot = data["bot"]
        if isinstance(bot, Bot):
            await notify_admins_pending(bot, admins, created)
