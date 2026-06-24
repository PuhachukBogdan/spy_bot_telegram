"""Broadcast + edit helpers for ops alerts (the proactive-write path).

Fans out to many groups concurrently under a Semaphore to respect Telegram's
~30 chats/sec limit. A failure in one chat never aborts the others — each send
/ edit is isolated and its outcome returned so the caller can persist state.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
from uuid import UUID

from aiogram import Bot
from aiogram.exceptions import TelegramAPIError

from src.utils.logging import get_logger

log = get_logger(__name__)


@dataclass(frozen=True)
class SendResult:
    chat_id: UUID
    telegram_message_id: int


@dataclass(frozen=True)
class EditResult:
    chat_id: UUID
    ok: bool
    reason: str | None = None


async def broadcast(
    bot: Bot,
    targets: list[tuple[UUID, int]],
    text: str,
    *,
    concurrency: int,
) -> list[SendResult]:
    """Send ``text`` to each (chat_uuid, telegram_chat_id); return the successes.

    Per-chat errors are logged and dropped, never raised.
    """
    sem = asyncio.Semaphore(max(1, concurrency))

    async def _one(chat_uuid: UUID, tg_chat_id: int) -> SendResult | None:
        async with sem:
            try:
                msg = await bot.send_message(tg_chat_id, text)
                return SendResult(chat_id=chat_uuid, telegram_message_id=msg.message_id)
            except TelegramAPIError as exc:
                log.warning("ops.broadcast.send_failed", chat_id=tg_chat_id, error=str(exc))
                return None

    results = await asyncio.gather(*(_one(u, c) for u, c in targets))
    return [r for r in results if r is not None]


async def edit_messages(
    bot: Bot,
    items: list[dict[str, object]],
    text: str,
    *,
    concurrency: int,
) -> list[EditResult]:
    """Edit each previously-posted message to ``text``.

    ``items`` rows carry ``chat_id`` (UUID), ``telegram_chat_id`` (int),
    ``telegram_message_id`` (int). An edit that fails because the message is
    unchanged is treated as success; any other failure (e.g. the 48h edit window
    has closed) is reported so the caller can flag it.
    """
    sem = asyncio.Semaphore(max(1, concurrency))

    async def _one(item: dict[str, object]) -> EditResult:
        chat_uuid = item["chat_id"]
        tg_chat_id = item["telegram_chat_id"]
        message_id = item["telegram_message_id"]
        assert isinstance(chat_uuid, UUID)
        assert isinstance(tg_chat_id, int)
        assert isinstance(message_id, int)
        async with sem:
            try:
                await bot.edit_message_text(
                    text, chat_id=tg_chat_id, message_id=message_id
                )
                return EditResult(chat_id=chat_uuid, ok=True)
            except TelegramAPIError as exc:
                reason = str(exc)
                if "message is not modified" in reason.lower():
                    return EditResult(chat_id=chat_uuid, ok=True)
                log.warning(
                    "ops.broadcast.edit_failed", chat_id=tg_chat_id, error=reason
                )
                return EditResult(chat_id=chat_uuid, ok=False, reason=reason)

    return list(await asyncio.gather(*(_one(i) for i in items)))
