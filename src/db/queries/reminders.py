"""reminders queries. Migration 0006.

Scheduled DM nudges to internal users. A worker polls
:func:`list_pending_to_fire` and flips fired rows via :func:`mark_sent`. Each
helper takes an already-acquired ``asyncpg.Connection`` (project-wide
convention; see ``chats.py``).
"""

from __future__ import annotations

from datetime import datetime
from uuid import UUID

import asyncpg

from src.db.models import Reminder


async def create(conn: asyncpg.Connection, payload: Reminder) -> Reminder:
    """Insert a new reminder; server fills ``id`` / ``created_at``."""
    row = await conn.fetchrow(
        """
        INSERT INTO reminders (
            target_user_id, partner_id, content, fire_at, status, created_by
        )
        VALUES ($1, $2, $3, $4, $5, $6)
        RETURNING *
        """,
        payload.target_user_id,
        payload.partner_id,
        payload.content,
        payload.fire_at,
        payload.status,
        payload.created_by,
    )
    assert row is not None  # INSERT ... RETURNING always yields a row
    return Reminder.from_record(row)


async def list_pending_to_fire(
    conn: asyncpg.Connection, now_utc: datetime
) -> list[Reminder]:
    """Return pending reminders whose ``fire_at`` has arrived, soonest first."""
    rows = await conn.fetch(
        """
        SELECT * FROM reminders
        WHERE status = 'pending' AND fire_at <= $1
        ORDER BY fire_at ASC
        """,
        now_utc,
    )
    return [Reminder.from_record(row) for row in rows]


async def mark_sent(conn: asyncpg.Connection, reminder_id: UUID) -> None:
    """Flag a reminder as sent (guarded to ``status='pending'`` for idempotency)."""
    await conn.execute(
        """
        UPDATE reminders
        SET status = 'sent', sent_at = now()
        WHERE id = $1 AND status = 'pending'
        """,
        reminder_id,
    )


async def cancel(conn: asyncpg.Connection, reminder_id: UUID) -> None:
    """Cancel a pending reminder (no-op if already sent/cancelled)."""
    await conn.execute(
        "UPDATE reminders SET status = 'cancelled' WHERE id = $1 AND status = 'pending'",
        reminder_id,
    )


async def list_by_user(
    conn: asyncpg.Connection, user_id: UUID, status: str = "pending"
) -> list[Reminder]:
    """Return a user's reminders in a given status, soonest fire_at first."""
    rows = await conn.fetch(
        """
        SELECT * FROM reminders
        WHERE target_user_id = $1 AND status = $2
        ORDER BY fire_at ASC
        """,
        user_id,
        status,
    )
    return [Reminder.from_record(row) for row in rows]


async def get_by_short_id(conn: asyncpg.Connection, short_id: str) -> Reminder | None:
    """Look a reminder up by the first 8 chars of its UUID (for terse DM commands)."""
    row = await conn.fetchrow(
        "SELECT * FROM reminders WHERE left(id::text, 8) = $1 LIMIT 1",
        short_id,
    )
    return Reminder.from_record(row) if row is not None else None
