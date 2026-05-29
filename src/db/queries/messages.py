"""messages / message_edits queries. Phase 5+.

Plain-SQL helpers over the ``messages`` and ``message_edits`` tables. Each takes
an already-acquired ``asyncpg.Connection`` so the caller controls the
pool/transaction boundary.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any
from uuid import UUID

import asyncpg

from src.db.models import Message


async def insert_message(
    conn: asyncpg.Connection,
    *,
    telegram_message_id: int,
    chat_id: UUID,
    sender_id: int | None,
    sender_chat_id: int | None,
    sender_name: str | None,
    sender_role: str,
    message_text: str | None,
    message_type: str,
    timestamp: datetime,
    reply_to_message_id: int | None = None,
    forward_from_id: int | None = None,
    forward_from_chat_id: int | None = None,
    message_thread_id: int | None = None,
    links: list[str] | None = None,
    mentions: list[str] | None = None,
    detected_language: str | None = None,
    is_significant: bool = False,
    raw_payload: dict[str, Any] | None = None,
) -> Message | None:
    """Insert one ingested message; return it, or ``None`` if it already existed.

    Idempotent via ``ON CONFLICT (chat_id, telegram_message_id) DO NOTHING``
    (CLAUDE.md 7.1 step 5 / 11.1): Telegram retries the same webhook for up to
    24h, so a duplicate insert is a no-op and returns ``None``. Tier-1 fields
    (``has_triggers`` / ``base_score`` / ``triggered_patterns``) keep their column
    defaults here and are filled by the Phase-6 matcher.
    """
    row = await conn.fetchrow(
        """
        INSERT INTO messages (
            telegram_message_id, chat_id, sender_id, sender_chat_id, sender_name,
            sender_role, message_text, message_type, timestamp,
            reply_to_message_id, forward_from_id, forward_from_chat_id,
            message_thread_id, links, mentions, detected_language,
            is_significant, raw_payload
        )
        VALUES (
            $1, $2, $3, $4, $5,
            $6, $7, $8, $9,
            $10, $11, $12,
            $13, $14, $15, $16,
            $17, $18
        )
        ON CONFLICT (chat_id, telegram_message_id) DO NOTHING
        RETURNING *
        """,
        telegram_message_id,
        chat_id,
        sender_id,
        sender_chat_id,
        sender_name,
        sender_role,
        message_text,
        message_type,
        timestamp,
        reply_to_message_id,
        forward_from_id,
        forward_from_chat_id,
        message_thread_id,
        links,
        mentions,
        detected_language,
        is_significant,
        raw_payload,
    )
    return Message.from_record(row) if row is not None else None


async def get_message(
    conn: asyncpg.Connection, chat_id: UUID, telegram_message_id: int
) -> Message | None:
    """Return a stored message by its (chat, telegram id) key, or ``None``.

    Served by the UNIQUE index on ``(chat_id, telegram_message_id)``. Used by the
    edit handler to find the original row before recording an edit.
    """
    row = await conn.fetchrow(
        """
        SELECT * FROM messages
        WHERE chat_id = $1 AND telegram_message_id = $2
        """,
        chat_id,
        telegram_message_id,
    )
    return Message.from_record(row) if row is not None else None


async def insert_message_edit(
    conn: asyncpg.Connection,
    *,
    message_id: UUID,
    old_text: str | None,
    new_text: str | None,
    edited_at: datetime,
) -> None:
    """Append an edit-history row for a message (CLAUDE.md 7.3)."""
    await conn.execute(
        """
        INSERT INTO message_edits (message_id, old_text, new_text, edited_at)
        VALUES ($1, $2, $3, $4)
        """,
        message_id,
        old_text,
        new_text,
        edited_at,
    )


async def update_message_text(
    conn: asyncpg.Connection, message_id: UUID, new_text: str | None
) -> None:
    """Overwrite a message's current text after an edit (history kept separately)."""
    await conn.execute(
        "UPDATE messages SET message_text = $2 WHERE id = $1",
        message_id,
        new_text,
    )
