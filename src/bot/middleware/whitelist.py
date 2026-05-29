"""Ignore messages from non-active chats. Phase 3.

Registered as an *outer* middleware on the message observer so it runs for every
incoming message before routing. Policy:

  - Private chats (DMs with staff) always pass — that's how commands work.
  - Group / supergroup messages pass only when the chat's ``status`` is
    ``'active'`` in the DB. Pending / banned / abandoned / unknown chats are
    dropped silently (CLAUDE.md 7.1 step 4: non-active chats are not processed).

Onboarding events (``my_chat_member`` / ``chat_member``) are *not* messages, so
they bypass this middleware and reach their Phase 4 handlers even while a chat is
still ``pending``.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from typing import Any

from aiogram import BaseMiddleware
from aiogram.enums import ChatType
from aiogram.types import Message, TelegramObject

from src.db.client import acquire_connection
from src.db.queries.chats import get_chat_status
from src.utils.logging import get_logger

log = get_logger(__name__)


class WhitelistMiddleware(BaseMiddleware):
    """Drop messages from chats that are not active partner chats."""

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

        async with acquire_connection() as conn:
            status = await get_chat_status(conn, chat.id)

        if status == "active":
            return await handler(event, data)

        # Not active: ignore. Debug level — this is the common steady state for
        # any chat we haven't authorized, not an error.
        log.debug("whitelist.skip", chat_id=chat.id, status=status or "unknown")
        return None
