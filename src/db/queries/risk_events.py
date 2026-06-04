"""risk_events queries. Phase 2+ (read/review surface for DM commands).

Listing + lookup + status-review helpers over ``risk_events``. Each takes an
already-acquired ``asyncpg.Connection`` (project-wide convention). Owner scoping
(a manager sees only their partners' risks) is pushed into SQL via an optional
``owner_id`` filter so the caller never over-fetches.
"""

from __future__ import annotations

from uuid import UUID

import asyncpg

from src.db.models import RiskEvent, RiskEventOverview


async def list_recent(
    conn: asyncpg.Connection,
    *,
    limit: int = 20,
    partner_id: UUID | None = None,
    owner_id: UUID | None = None,
) -> list[RiskEventOverview]:
    """Most-recent risk events (newest first), with partner name.

    ``partner_id`` narrows to one partner; ``owner_id`` restricts to a manager's
    own partners (admins pass ``None``). Both filters are parameterized and AND
    together, so a manager asking for a specific partner still can't see one they
    don't own.
    """
    rows = await conn.fetch(
        """
        SELECT r.id, r.risk_level, r.risk_type, r.detected_phrase, r.status,
               r.created_at, p.name AS partner_name
        FROM risk_events r
        LEFT JOIN partners p ON p.id = r.partner_id
        WHERE ($1::uuid IS NULL OR r.partner_id = $1)
          AND ($2::uuid IS NULL
               OR r.partner_id IN (
                   SELECT id FROM partners WHERE owner_manager_id = $2))
        ORDER BY r.created_at DESC
        LIMIT $3
        """,
        partner_id,
        owner_id,
        limit,
    )
    return [RiskEventOverview.from_record(row) for row in rows]


async def list_by_chat(
    conn: asyncpg.Connection, chat_id: UUID, limit: int = 5
) -> list[RiskEventOverview]:
    """Most-recent risk events for one chat unit (newest first), with partner name."""
    rows = await conn.fetch(
        """
        SELECT r.id, r.risk_level, r.risk_type, r.detected_phrase, r.status,
               r.created_at, p.name AS partner_name
        FROM risk_events r
        LEFT JOIN partners p ON p.id = r.partner_id
        WHERE r.chat_id = $1
        ORDER BY r.created_at DESC
        LIMIT $2
        """,
        chat_id,
        limit,
    )
    return [RiskEventOverview.from_record(row) for row in rows]


async def get_by_ref(conn: asyncpg.Connection, ref: str) -> RiskEvent | None:
    """Look up a risk event by its full UUID or its first 8 chars (``/risk``).

    ``ref`` is compared as text against the canonical (lowercase) UUID form, so
    the caller should lowercase it. Returns the full row for the detail card.
    """
    row = await conn.fetchrow(
        "SELECT * FROM risk_events WHERE id::text = $1 OR left(id::text, 8) = $1 LIMIT 1",
        ref,
    )
    return RiskEvent.from_record(row) if row is not None else None


async def update_status(
    conn: asyncpg.Connection, risk_event_id: UUID, status: str, reviewed_by: UUID
) -> RiskEvent | None:
    """Set a risk event's review status (``/mark_*``); stamp reviewer + time.

    Returns the updated row, or ``None`` if the id was unknown.
    """
    row = await conn.fetchrow(
        """
        UPDATE risk_events
        SET status = $2, reviewed_by = $3, reviewed_at = now()
        WHERE id = $1
        RETURNING *
        """,
        risk_event_id,
        status,
        reviewed_by,
    )
    return RiskEvent.from_record(row) if row is not None else None
