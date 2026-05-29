"""chats queries. Phase 2+.

Plain-SQL helpers over the ``chats`` table. Each takes an already-acquired
``asyncpg.Connection`` so the caller controls the pool/transaction boundary.
"""

from __future__ import annotations

from datetime import datetime
from typing import cast
from uuid import UUID

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


async def create_pending_chat(
    conn: asyncpg.Connection,
    telegram_chat_id: int,
    chat_name: str | None,
    added_by_user_id: int | None,
) -> Chat | None:
    """Insert a freshly-added chat as ``status='pending'`` (onboarding step).

    Idempotent against Telegram's webhook retries and re-adds via
    ``ON CONFLICT (telegram_chat_id) DO NOTHING``: returns the new ``Chat`` row on
    first insert, or ``None`` if the chat already exists (CLAUDE.md 7.2 / 11.1).
    The caller uses the ``None`` result to skip re-notifying admins.
    """
    row = await conn.fetchrow(
        """
        INSERT INTO chats (telegram_chat_id, chat_name, added_by_user_id, status)
        VALUES ($1, $2, $3, 'pending')
        ON CONFLICT (telegram_chat_id) DO NOTHING
        RETURNING *
        """,
        telegram_chat_id,
        chat_name,
        added_by_user_id,
    )
    return Chat.from_record(row) if row is not None else None


async def list_pending_chats(conn: asyncpg.Connection) -> list[Chat]:
    """Return all chats awaiting authorization, oldest first (for ``/pending``)."""
    rows = await conn.fetch(
        "SELECT * FROM chats WHERE status = 'pending' ORDER BY created_at ASC"
    )
    return [Chat.from_record(row) for row in rows]


async def authorize_chat(
    conn: asyncpg.Connection,
    telegram_chat_id: int,
    partner_id: UUID,
    authorized_by: UUID,
) -> Chat | None:
    """Activate a pending chat and bind it to a partner (``/authorize``).

    Only flips a chat that is still ``'pending'`` (the WHERE guard makes a double
    ``/authorize`` a no-op that returns ``None``), stamping
    ``authorized_by`` / ``authorized_at`` for the audit trail (CLAUDE.md 7.2).
    """
    row = await conn.fetchrow(
        """
        UPDATE chats
        SET status = 'active',
            partner_id = $2,
            authorized_by = $3,
            authorized_at = now()
        WHERE telegram_chat_id = $1
          AND status = 'pending'
        RETURNING *
        """,
        telegram_chat_id,
        partner_id,
        authorized_by,
    )
    return Chat.from_record(row) if row is not None else None


async def reject_chat(conn: asyncpg.Connection, telegram_chat_id: int) -> Chat | None:
    """Ban a pending chat (``/reject``); caller then calls ``bot.leave_chat``.

    Guarded to ``'pending'`` so an already-active or already-banned chat is not
    silently re-banned. Returns the updated row, or ``None`` if nothing matched.
    """
    row = await conn.fetchrow(
        """
        UPDATE chats
        SET status = 'banned'
        WHERE telegram_chat_id = $1
          AND status = 'pending'
        RETURNING *
        """,
        telegram_chat_id,
    )
    return Chat.from_record(row) if row is not None else None


async def list_stale_pending_chats(
    conn: asyncpg.Connection, older_than: datetime
) -> list[Chat]:
    """Return pending chats created before ``older_than`` (abandoned-chat sweep)."""
    rows = await conn.fetch(
        """
        SELECT *
        FROM chats
        WHERE status = 'pending'
          AND created_at < $1
        ORDER BY created_at ASC
        """,
        older_than,
    )
    return [Chat.from_record(row) for row in rows]


async def mark_chat_abandoned(
    conn: asyncpg.Connection, telegram_chat_id: int
) -> Chat | None:
    """Flag a stale pending chat as ``'abandoned'`` after the bot leaves it."""
    row = await conn.fetchrow(
        """
        UPDATE chats
        SET status = 'abandoned'
        WHERE telegram_chat_id = $1
          AND status = 'pending'
        RETURNING *
        """,
        telegram_chat_id,
    )
    return Chat.from_record(row) if row is not None else None


async def update_chat_telegram_id(
    conn: asyncpg.Connection, old_telegram_chat_id: int, new_telegram_chat_id: int
) -> Chat | None:
    """Repoint a chat to its new id after a group->supergroup migration.

    Telegram assigns a fresh chat id on migration; without this the old row would
    orphan and the new id would look like an unknown chat (CLAUDE.md 11.6). The
    UNIQUE constraint on ``telegram_chat_id`` means this is a no-op (``None``)
    if the new id is already recorded.
    """
    row = await conn.fetchrow(
        """
        UPDATE chats
        SET telegram_chat_id = $2
        WHERE telegram_chat_id = $1
        RETURNING *
        """,
        old_telegram_chat_id,
        new_telegram_chat_id,
    )
    return Chat.from_record(row) if row is not None else None
