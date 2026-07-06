"""activity_signals queries. Migration 0011.

Stores LLM-detected manager proposals and closed deals for the manager-centric
weekly/monthly summary (Phase 16). Each function takes an already-acquired
``asyncpg.Connection`` (project-wide convention).
"""

from __future__ import annotations

from datetime import datetime
from uuid import UUID

import asyncpg

from src.db.models import ActivitySignalRow


async def save_activity_signal(
    conn: asyncpg.Connection,
    *,
    chat_id: UUID,
    message_id: UUID | None,
    sender_id: int | None,
    signal_type: str,
    description: str | None,
) -> ActivitySignalRow:
    """Insert one activity signal and return the stored row."""
    row = await conn.fetchrow(
        """
        INSERT INTO activity_signals
            (chat_id, message_id, sender_id, signal_type, description)
        VALUES ($1, $2, $3, $4, $5)
        RETURNING *
        """,
        chat_id,
        message_id,
        sender_id,
        signal_type,
        description,
    )
    return ActivitySignalRow.from_record(row)


async def count_proposals(
    conn: asyncpg.Connection,
    since: datetime,
    until: datetime,
) -> int:
    """Portfolio-wide count of manager proposals in [since, until).

    A top-line metric for the weekly/monthly summary (Phase 16). Counts across
    all chats — proposals are keyed by ``sender_id``/``chat``, not by owning
    manager, so this is a portfolio total, not a per-manager breakdown.
    """
    row = await conn.fetchrow(
        """
        SELECT COUNT(*)::int AS n
        FROM activity_signals
        WHERE signal_type = 'manager_proposal'
          AND created_at >= $1
          AND created_at < $2
        """,
        since,
        until,
    )
    return int(row["n"]) if row else 0


async def list_by_sender_since(
    conn: asyncpg.Connection,
    *,
    sender_id: int,
    since: datetime,
    until: datetime,
) -> list[ActivitySignalRow]:
    """All signals for one sender (Telegram user ID) in [since, until).

    Used by the summary builder (Phase 16) to aggregate proposals and
    closed deals per manager for the report period.
    """
    rows = await conn.fetch(
        """
        SELECT * FROM activity_signals
        WHERE sender_id = $1
          AND created_at >= $2
          AND created_at < $3
        ORDER BY created_at
        """,
        sender_id,
        since,
        until,
    )
    return [ActivitySignalRow.from_record(r) for r in rows]
