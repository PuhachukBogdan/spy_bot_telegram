"""partners queries. Phase 2+.

Plain-SQL helpers over the ``partners`` table. Each takes an already-acquired
``asyncpg.Connection`` so the caller controls the pool/transaction boundary.
"""

from __future__ import annotations

import asyncpg

from src.db.models import Partner


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
