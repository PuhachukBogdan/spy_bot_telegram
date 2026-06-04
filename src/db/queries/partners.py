"""partners queries. Phase 2+.

Plain-SQL helpers over the ``partners`` table. Each takes an already-acquired
``asyncpg.Connection`` so the caller controls the pool/transaction boundary.
"""

from __future__ import annotations

from uuid import UUID

import asyncpg

from src.db.models import Partner, PartnerOverview


async def list_partners(
    conn: asyncpg.Connection,
    *,
    status: str | None = None,
    owner_id: UUID | None = None,
) -> list[PartnerOverview]:
    """List partners with activity rollups for ``/partners`` (newest activity first).

    ``status`` filters by partner status (``None`` = all). ``owner_id`` restricts to
    one manager's partners (managers see only their own; admins pass ``None``).
    ``active_chats`` counts only active units; ``last_activity`` is the latest
    message timestamp across all the partner's chats. Both filters parameterized.
    """
    rows = await conn.fetch(
        """
        SELECT p.id, p.name, p.status, p.owner_manager_id,
               count(DISTINCT c.id) FILTER (WHERE c.status = 'active') AS active_chats,
               max(m.timestamp) AS last_activity
        FROM partners p
        LEFT JOIN chats c ON c.partner_id = p.id
        LEFT JOIN messages m ON m.chat_id = c.id
        WHERE ($1::text IS NULL OR p.status = $1)
          AND ($2::uuid IS NULL OR p.owner_manager_id = $2)
        GROUP BY p.id
        ORDER BY max(m.timestamp) DESC NULLS LAST, p.name ASC
        """,
        status,
        owner_id,
    )
    return [PartnerOverview.from_record(row) for row in rows]


async def update_partner_status(
    conn: asyncpg.Connection, partner_id: UUID, status: str
) -> Partner | None:
    """Set a partner's status (``/set_partner_status``); return the row or ``None``."""
    row = await conn.fetchrow(
        """
        UPDATE partners
        SET status = $2, updated_at = now()
        WHERE id = $1
        RETURNING *
        """,
        partner_id,
        status,
    )
    return Partner.from_record(row) if row is not None else None


async def update_partner_owner(
    conn: asyncpg.Connection, partner_id: UUID, owner_id: UUID | None
) -> Partner | None:
    """Set a partner's owning manager (``/set_owner``); return the row or ``None``."""
    row = await conn.fetchrow(
        """
        UPDATE partners
        SET owner_manager_id = $2, updated_at = now()
        WHERE id = $1
        RETURNING *
        """,
        partner_id,
        owner_id,
    )
    return Partner.from_record(row) if row is not None else None


async def get_partner_by_id(conn: asyncpg.Connection, partner_id: UUID) -> Partner | None:
    """Return a partner by primary key, or ``None``."""
    row = await conn.fetchrow("SELECT * FROM partners WHERE id = $1", partner_id)
    return Partner.from_record(row) if row is not None else None


async def get_partner_by_name(conn: asyncpg.Connection, name: str) -> Partner | None:
    """Return the partner with this ``name``, or ``None`` if none exists.

    Read-only counterpart to :func:`get_or_create_partner`, used for access checks
    (``require_partner_access``) where we must NOT create a partner as a side
    effect of looking one up. ``partners.name`` is UNIQUE, so this is a point read.
    """
    row = await conn.fetchrow("SELECT * FROM partners WHERE name = $1", name)
    return Partner.from_record(row) if row is not None else None


async def get_or_create_partner(conn: asyncpg.Connection, name: str) -> Partner:
    """Return the partner with this ``name``, creating it if absent.

    Used by ``/authorize <chat_id> <partner_name>``: a chat may be the first one
    we see for a partner, or join an existing partner. ``partners.name`` is
    UNIQUE, so the INSERT races safely: ``ON CONFLICT (name) DO NOTHING`` followed
    by a SELECT fallback yields the existing row when a concurrent caller won.
    """
    row = await conn.fetchrow(
        """
        INSERT INTO partners (name)
        VALUES ($1)
        ON CONFLICT (name) DO NOTHING
        RETURNING *
        """,
        name,
    )
    if row is None:
        row = await conn.fetchrow("SELECT * FROM partners WHERE name = $1", name)
    assert row is not None  # either the INSERT or the SELECT returns the row
    return Partner.from_record(row)
