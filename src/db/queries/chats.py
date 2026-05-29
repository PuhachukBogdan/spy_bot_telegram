"""chats queries. Phase 2+.

Plain-SQL helpers over the ``chats`` table. Each takes an already-acquired
``asyncpg.Connection`` so the caller controls the pool/transaction boundary.
"""

from __future__ import annotations

from typing import cast

import asyncpg

from src.db.models import Chat


async def get_chat_status(conn: asyncpg.Connection, telegram_chat_id: int) -> str | None:
    """Return ``chats.status`` for a Telegram chat id, or ``None`` if unknown.

    Used by the whitelist middleware to gate ingestion: only ``'active'`` chats
    are processed. Backed by the UNIQUE index on ``telegram_chat_id``.
    """
    # asyncpg is untyped, so fetchval is Any; the column is TEXT NOT NULL.
    return cast(
        "str | None",
        await conn.fetchval(
            "SELECT status FROM chats WHERE telegram_chat_id = $1",
            telegram_chat_id,
        ),
    )


async def get_chat_by_telegram_id(
    conn: asyncpg.Connection, telegram_chat_id: int
) -> Chat | None:
    """Return the full ``Chat`` row for a Telegram chat id, or ``None``."""
    row = await conn.fetchrow(
        "SELECT * FROM chats WHERE telegram_chat_id = $1",
        telegram_chat_id,
    )
    return Chat.from_record(row) if row is not None else None
