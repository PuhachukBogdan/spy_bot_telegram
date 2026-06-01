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
from src.db.queries.chats import create_pending_chat, get_chat_status
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

        thread_id = effective_topic_id(event)
        async with acquire_connection() as conn:
            status = await get_chat_status(conn, chat.id, thread_id)

        if status == "active":
            return await handler(event, data)

        # Unknown forum topic: discover it (create pending + notify) so an admin
        # can authorize it. Group-level units (thread_id None) are created by the
        # onboarding handler, not here.
        if status is None and thread_id is not None:
            await self._discover_topic(event, thread_id, data)

        # Not active: ignore. Debug level — the common steady state for any unit
        # we haven't authorized, not an error.
        log.debug(
            "whitelist.skip",
            chat_id=chat.id,
            thread_id=thread_id,
            status=status or "unknown",
        )
        return None

    async def _discover_topic(
        self, message: Message, thread_id: int, data: dict[str, Any]
    ) -> None:
        """Register a newly-seen forum topic as pending and DM admins once.

        Idempotent via ``create_pending_chat``'s ON CONFLICT: only the first
        message in a topic creates the row and notifies; later ones see a
        ``'pending'`` status and never reach here again.
        """
        actor = message.from_user
        async with acquire_connection() as conn:
            created = await create_pending_chat(
                conn,
                telegram_chat_id=message.chat.id,
                thread_id=thread_id,
                chat_name=message.chat.title,
                added_by_user_id=actor.id if actor is not None else None,
            )
            if created is None:  # lost a race; already discovered
                return
            admins = await list_admin_users(conn)

        log.info(
            "onboarding.topic_discovered",
            chat_id=message.chat.id,
            thread_id=thread_id,
            chat_name=message.chat.title,
        )
        bot = data["bot"]
        if isinstance(bot, Bot):
            await notify_admins_pending(bot, admins, created)
