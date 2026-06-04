"""chats queries. Phase 2+ (topic-aware since the forum-topics change).

Plain-SQL helpers over the ``chats`` table. A row is a monitored *unit* =
``(telegram_chat_id, topic)`` where ``topic`` is a forum topic id or ``None``
for the whole group (see ``supabase/migrations/0005_topic_units.sql`` and the
wiki plan ``proj1-tgbot-topic-separation-plan``). Lookups match on the generated
``topic_key = COALESCE(message_thread_id, 0)`` so a ``None`` thread is NULL-safe.

Each helper takes an already-acquired ``asyncpg.Connection`` so the caller
controls the pool/transaction boundary.
"""

from __future__ import annotations

from datetime import datetime
from typing import cast
from uuid import UUID

import asyncpg

from src.db.models import Chat


async def get_chat_status(
    conn: asyncpg.Connection, telegram_chat_id: int, thread_id: int | None
) -> str | None:
    """Return ``chats.status`` for a monitored unit, or ``None`` if unknown.

    Used by the whitelist middleware to gate ingestion: only ``'active'`` units
    are processed. Backed by the UNIQUE index on ``(telegram_chat_id, topic_key)``.
    """
    # asyncpg is untyped, so fetchval is Any; the column is TEXT NOT NULL.
    return cast(
        "str | None",
        await conn.fetchval(
            """
            SELECT status FROM chats
            WHERE telegram_chat_id = $1 AND topic_key = COALESCE($2, 0)
            """,
            telegram_chat_id,
            thread_id,
        ),
    )


async def get_chat_unit(
    conn: asyncpg.Connection, telegram_chat_id: int, thread_id: int | None
) -> Chat | None:
    """Return the full ``Chat`` row for a monitored unit, or ``None``."""
    row = await conn.fetchrow(
        """
        SELECT * FROM chats
        WHERE telegram_chat_id = $1 AND topic_key = COALESCE($2, 0)
        """,
        telegram_chat_id,
        thread_id,
    )
    return Chat.from_record(row) if row is not None else None


async def get_by_unit(
    conn: asyncpg.Connection, telegram_chat_id: int, topic_key: int = 0
) -> Chat | None:
    """Return the ``Chat`` for a unit by its stored ``topic_key`` directly.

    Complements :func:`get_chat_unit`: that one takes a possibly-``None`` thread
    id and NULL-coalesces it; this one takes the already-resolved ``topic_key``
    (0 = whole group / General topic).
    """
    row = await conn.fetchrow(
        "SELECT * FROM chats WHERE telegram_chat_id = $1 AND topic_key = $2",
        telegram_chat_id,
        topic_key,
    )
    return Chat.from_record(row) if row is not None else None


async def create_pending_chat(
    conn: asyncpg.Connection,
    telegram_chat_id: int,
    thread_id: int | None,
    chat_name: str | None,
    added_by_user_id: int | None,
    topic_name: str | None = None,
    unit_type: str = "group",
) -> Chat | None:
    """Insert a freshly-discovered unit as ``status='pending'`` (onboarding step).

    ``unit_type`` is ``'group'`` for a group-level unit (bot added to a group) or
    ``'topic'`` for a forum topic discovered from its first message.

    Idempotent against Telegram's webhook retries, re-adds, and repeated messages
    in the same topic via ``ON CONFLICT (telegram_chat_id, topic_key) DO NOTHING``:
    returns the new ``Chat`` row on first insert, or ``None`` if the unit already
    exists. The caller uses the ``None`` result to skip re-notifying admins.
    """
    row = await conn.fetchrow(
        """
        INSERT INTO chats (
            telegram_chat_id, message_thread_id, topic_name,
            chat_name, added_by_user_id, status, unit_type
        )
        VALUES ($1, $2, $3, $4, $5, 'pending', $6)
        ON CONFLICT (telegram_chat_id, topic_key) DO NOTHING
        RETURNING *
        """,
        telegram_chat_id,
        thread_id,
        topic_name,
        chat_name,
        added_by_user_id,
        unit_type,
    )
    return Chat.from_record(row) if row is not None else None


async def list_pending(conn: asyncpg.Connection) -> list[Chat]:
    """Return all units awaiting authorization, oldest first (for ``/pending``)."""
    rows = await conn.fetch(
        "SELECT * FROM chats WHERE status = 'pending' ORDER BY created_at ASC"
    )
    return [Chat.from_record(row) for row in rows]


async def list_pending_chats(conn: asyncpg.Connection) -> list[Chat]:
    """Backwards-compatible alias for :func:`list_pending` (used by ``/pending``)."""
    return await list_pending(conn)


async def list_pending_topics(conn: asyncpg.Connection) -> list[Chat]:
    """Pending units that are forum topics specifically (``unit_type = 'topic'``).

    ``unit_type`` is stamped by the onboarding/ingestion layer (migration 0006);
    until that layer writes it, freshly-discovered units default to ``'group'``,
    so this returns rows only once topic typing is actually being set.
    """
    rows = await conn.fetch(
        """
        SELECT * FROM chats
        WHERE status = 'pending' AND unit_type = 'topic'
        ORDER BY created_at ASC
        """
    )
    return [Chat.from_record(row) for row in rows]


async def authorize_chat(
    conn: asyncpg.Connection,
    telegram_chat_id: int,
    thread_id: int | None,
    partner_id: UUID,
    authorized_by: UUID,
) -> Chat | None:
    """Activate a pending unit and bind it to a partner (``/authorize``).

    Only flips a unit that is still ``'pending'`` (the WHERE guard makes a double
    ``/authorize`` a no-op that returns ``None``), stamping ``authorized_by`` /
    ``authorized_at`` for the audit trail.
    """
    row = await conn.fetchrow(
        """
        UPDATE chats
        SET status = 'active',
            partner_id = $3,
            authorized_by = $4,
            authorized_at = now()
        WHERE telegram_chat_id = $1
          AND topic_key = COALESCE($2, 0)
          AND status = 'pending'
        RETURNING *
        """,
        telegram_chat_id,
        thread_id,
        partner_id,
        authorized_by,
    )
    return Chat.from_record(row) if row is not None else None


async def reject_chat(
    conn: asyncpg.Connection, telegram_chat_id: int, thread_id: int | None
) -> Chat | None:
    """Ban a pending unit (``/reject``); the caller decides whether to leave.

    Guarded to ``'pending'`` so an already-active or already-banned unit is not
    silently re-banned. Returns the updated row, or ``None`` if nothing matched.
    """
    row = await conn.fetchrow(
        """
        UPDATE chats
        SET status = 'banned'
        WHERE telegram_chat_id = $1
          AND topic_key = COALESCE($2, 0)
          AND status = 'pending'
        RETURNING *
        """,
        telegram_chat_id,
        thread_id,
    )
    return Chat.from_record(row) if row is not None else None


async def reject_topic(
    conn: asyncpg.Connection, telegram_chat_id: int, thread_id: int | None
) -> Chat | None:
    """Reject a pending forum-topic unit (``/reject_topic``).

    Sets ``status='rejected'`` (distinct from a group-level ``'banned'``): a
    rejected topic does NOT make the bot leave the supergroup — it stays for the
    other topics. Guarded to a still-``'pending'`` row whose ``unit_type='topic'``
    so it can never flip a group-level unit. Returns the row, or ``None``.
    """
    row = await conn.fetchrow(
        """
        UPDATE chats
        SET status = 'rejected'
        WHERE telegram_chat_id = $1
          AND topic_key = COALESCE($2, 0)
          AND status = 'pending'
          AND unit_type = 'topic'
        RETURNING *
        """,
        telegram_chat_id,
        thread_id,
    )
    return Chat.from_record(row) if row is not None else None


async def count_live_units(conn: asyncpg.Connection, telegram_chat_id: int) -> int:
    """Count not-yet-dismissed units of a supergroup (status pending or active).

    Used to decide whether leaving the whole Telegram supergroup is safe:
    rejecting/abandoning one topic must NOT make the bot leave a supergroup that
    still monitors other topics (leaving would kill them all).
    """
    return cast(
        "int",
        await conn.fetchval(
            """
            SELECT count(*) FROM chats
            WHERE telegram_chat_id = $1 AND status IN ('pending', 'active')
            """,
            telegram_chat_id,
        ),
    )


async def list_stale_pending_chats(
    conn: asyncpg.Connection, older_than: datetime
) -> list[Chat]:
    """Return pending units created before ``older_than`` (abandoned-chat sweep)."""
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
    conn: asyncpg.Connection, telegram_chat_id: int, thread_id: int | None
) -> Chat | None:
    """Flag a stale pending unit as ``'abandoned'`` (abandoned-chat sweep)."""
    row = await conn.fetchrow(
        """
        UPDATE chats
        SET status = 'abandoned'
        WHERE telegram_chat_id = $1
          AND topic_key = COALESCE($2, 0)
          AND status = 'pending'
        RETURNING *
        """,
        telegram_chat_id,
        thread_id,
    )
    return Chat.from_record(row) if row is not None else None


async def update_chat_telegram_id(
    conn: asyncpg.Connection, old_telegram_chat_id: int, new_telegram_chat_id: int
) -> int:
    """Repoint every unit of a supergroup to its new id after a migration.

    Telegram assigns a fresh chat id on group->supergroup migration; without this
    the old rows orphan and the new id looks unknown (CLAUDE.md 11.6). All topic
    units of the supergroup move together (``topic_key`` is unchanged, so the new
    ``(telegram_chat_id, topic_key)`` pairs stay unique). Returns the row count.
    """
    result = await conn.execute(
        "UPDATE chats SET telegram_chat_id = $2 WHERE telegram_chat_id = $1",
        old_telegram_chat_id,
        new_telegram_chat_id,
    )
    # asyncpg execute returns a tag like "UPDATE 3"; take the trailing count.
    return int(result.split()[-1]) if result else 0
