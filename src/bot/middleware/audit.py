"""audit_log for each command. Phase 3.

Emits a structured ``audit.command`` log line for every command message (text or
caption starting with ``/``). Registered as an *outer* middleware so it sees a
command even if no handler matches it (useful for spotting unknown or
out-of-place command attempts).

Scope note: this writes to the structured log, not to the ``admin_audit_log``
table. Read-only commands (``/start``, ``/help``, ``/whoami``) don't belong in a
DB audit trail. State-changing admin actions (authorize / reject / mark_*) get an
explicit ``admin_audit_log`` row written by their own handlers in Phase 4 and
Phase 13, where the affected ``target_id`` is known.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from typing import Any

from aiogram import BaseMiddleware
from aiogram.types import Message, TelegramObject

from src.utils.logging import get_logger

log = get_logger(__name__)


class AuditMiddleware(BaseMiddleware):
    """Log every command invocation with caller and chat context."""

    async def __call__(
        self,
        handler: Callable[[TelegramObject, dict[str, Any]], Awaitable[Any]],
        event: TelegramObject,
        data: dict[str, Any],
    ) -> Any:
        if isinstance(event, Message):
            text = event.text or event.caption
            if text and text.startswith("/"):
                user = event.from_user
                log.info(
                    "audit.command",
                    command=text.split(maxsplit=1)[0],
                    tg_user_id=user.id if user else None,
                    username=user.username if user else None,
                    chat_id=event.chat.id,
                    chat_type=event.chat.type,
                )
        return await handler(event, data)
