"""partner_contacts queries. Migration 0006.

Maps a Telegram ``user_id`` to a partner so business-mode DMs can be attributed.
Each helper takes an already-acquired ``asyncpg.Connection`` (project-wide
convention; see ``chats.py``).
"""

from __future__ import annotations

from uuid import UUID

import asyncpg

from src.db.models import PartnerContact


async def get_by_telegram_user_id(
    conn: asyncpg.Connection, user_id: int
) -> PartnerContact | None:
    """Return the contact for a Telegram ``user_id``, or ``None``.

    ``telegram_user_id`` is UNIQUE, so this is a point lookup.
    """
    row = await conn.fetchrow(
        "SELECT * FROM partner_contacts WHERE telegram_user_id = $1",
        user_id,
    )
    return PartnerContact.from_record(row) if row is not None else None


async def create(
    conn: asyncpg.Connection, payload: PartnerContact
) -> PartnerContact:
    """Insert a new partner contact; server fills ``id`` / ``created_at``."""
    row = await conn.fetchrow(
        """
        INSERT INTO partner_contacts (partner_id, telegram_user_id, full_name, notes)
        VALUES ($1, $2, $3, $4)
        RETURNING *
        """,
        payload.partner_id,
        payload.telegram_user_id,
        payload.full_name,
        payload.notes,
    )
    assert row is not None  # INSERT ... RETURNING always yields a row
    return PartnerContact.from_record(row)


async def list_by_partner(
    conn: asyncpg.Connection, partner_id: UUID
) -> list[PartnerContact]:
    """Return all contacts for a partner, newest first."""
    rows = await conn.fetch(
        "SELECT * FROM partner_contacts WHERE partner_id = $1 ORDER BY created_at DESC",
        partner_id,
    )
    return [PartnerContact.from_record(row) for row in rows]
