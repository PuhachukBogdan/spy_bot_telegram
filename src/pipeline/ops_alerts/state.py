"""DB state for the payment-incident monitor (migration 0016).

Plain-SQL helpers over ``payment_incidents`` and ``payment_incident_messages``.
Each takes an already-acquired ``asyncpg.Connection`` (callers own the txn).
"""

from __future__ import annotations

from datetime import date, datetime
from typing import Any
from uuid import UUID

import asyncpg


async def count_incidents(conn: asyncpg.Connection) -> int:
    """Total rows in ``payment_incidents`` (used to detect the first run)."""
    row = await conn.fetchrow("SELECT COUNT(*)::int AS n FROM payment_incidents")
    assert row is not None
    return int(row["n"])


async def get_incident(
    conn: asyncpg.Connection, incident_id: str
) -> dict[str, Any] | None:
    """Return the incident row as a dict, or None if unseen."""
    row = await conn.fetchrow(
        "SELECT * FROM payment_incidents WHERE incident_id = $1", incident_id
    )
    return dict(row) if row is not None else None


async def insert_incident(
    conn: asyncpg.Connection,
    *,
    incident_id: str,
    country: str,
    provider: str,
    issue: str | None,
    link: str | None,
    details: str | None,
    status: str,
    iso_date: datetime | None,
    last_update: datetime,
    seeded_only: bool,
) -> None:
    """Insert a new incident. ``seeded_only`` marks first-run rows not broadcast.

    Idempotent: a concurrent insert of the same incident_id is a no-op.
    """
    await conn.execute(
        """
        INSERT INTO payment_incidents (
            incident_id, country, provider, issue, link, details,
            status, iso_date, last_update, seeded_only
        )
        VALUES ($1,$2,$3,$4,$5,$6,$7,$8,$9,$10)
        ON CONFLICT (incident_id) DO NOTHING
        """,
        incident_id, country, provider, issue, link, details,
        status, iso_date, last_update, seeded_only,
    )


async def update_incident(
    conn: asyncpg.Connection,
    *,
    incident_id: str,
    status: str,
    last_update: datetime,
    details: str | None,
) -> None:
    """Update a known incident's mutable fields (status / details / last_update)."""
    await conn.execute(
        """
        UPDATE payment_incidents
        SET status = $2, last_update = $3, details = $4
        WHERE incident_id = $1
        """,
        incident_id, status, last_update, details,
    )


async def record_message(
    conn: asyncpg.Connection,
    *,
    incident_id: str,
    chat_id: UUID,
    telegram_message_id: int,
) -> None:
    """Record a posted message so a later update can edit it in place."""
    await conn.execute(
        """
        INSERT INTO payment_incident_messages (
            incident_id, chat_id, telegram_message_id
        )
        VALUES ($1, $2, $3)
        ON CONFLICT (incident_id, chat_id) DO UPDATE SET
            telegram_message_id = EXCLUDED.telegram_message_id,
            posted_at = now(), edit_failed = false, edit_failure_reason = NULL
        """,
        incident_id, chat_id, telegram_message_id,
    )


async def list_incident_messages(
    conn: asyncpg.Connection, incident_id: str
) -> list[dict[str, Any]]:
    """Posted messages for an incident, joined to the live Telegram chat id.

    Returns rows: ``chat_id`` (UUID), ``telegram_chat_id`` (BIGINT, for the edit
    call), ``telegram_message_id`` (BIGINT). Skips messages whose chat row is gone.
    """
    rows = await conn.fetch(
        """
        SELECT m.chat_id, c.telegram_chat_id, m.telegram_message_id
        FROM payment_incident_messages m
        JOIN chats c ON c.id = m.chat_id
        WHERE m.incident_id = $1
        """,
        incident_id,
    )
    return [dict(r) for r in rows]


async def mark_message_edited(
    conn: asyncpg.Connection, *, incident_id: str, chat_id: UUID
) -> None:
    """Stamp a successful edit."""
    await conn.execute(
        """
        UPDATE payment_incident_messages
        SET last_edited_at = now(), edit_failed = false, edit_failure_reason = NULL
        WHERE incident_id = $1 AND chat_id = $2
        """,
        incident_id, chat_id,
    )


async def mark_message_edit_failed(
    conn: asyncpg.Connection, *, incident_id: str, chat_id: UUID, reason: str
) -> None:
    """Flag a failed edit (e.g. message older than Telegram's 48h edit window)."""
    await conn.execute(
        """
        UPDATE payment_incident_messages
        SET edit_failed = true, edit_failure_reason = $3
        WHERE incident_id = $1 AND chat_id = $2
        """,
        incident_id, chat_id, reason[:500],
    )


# --- Holiday reminder dedup (ops_holiday_sends) ------------------------------


async def holiday_already_sent(
    conn: asyncpg.Connection, holiday_date: date, holiday_name: str
) -> bool:
    """True if the reminder for this (date, name) holiday was already sent."""
    row = await conn.fetchrow(
        """
        SELECT 1 FROM ops_holiday_sends
        WHERE holiday_date = $1 AND holiday_name = $2
        """,
        holiday_date, holiday_name,
    )
    return row is not None


async def record_holiday_sent(
    conn: asyncpg.Connection, holiday_date: date, holiday_name: str
) -> bool:
    """Record that we reminded about a holiday. Returns False if already recorded.

    The INSERT ... ON CONFLICT DO NOTHING makes the send-claim atomic: only the
    caller that inserts the row (RETURNING a value) should broadcast, so two
    overlapping ticks never both post.
    """
    row = await conn.fetchrow(
        """
        INSERT INTO ops_holiday_sends (holiday_date, holiday_name)
        VALUES ($1, $2)
        ON CONFLICT (holiday_date, holiday_name) DO NOTHING
        RETURNING holiday_date
        """,
        holiday_date, holiday_name,
    )
    return row is not None
