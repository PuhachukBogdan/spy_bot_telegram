"""business_connections queries. Migration 0006 (Telegram Business mode).

Plain-SQL helpers over the ``business_connections`` table. Each takes an
already-acquired ``asyncpg.Connection`` so the caller controls the
pool/transaction boundary — the same convention as the rest of
``src/db/queries`` (see ``chats.py``).
"""

from __future__ import annotations

from typing import Any

import asyncpg

from src.db.models import BusinessConnection

# Columns ``update_status`` is allowed to set besides ``status``. Whitelisted so
# arbitrary ``**fields`` keys can never be interpolated into the SET clause.
_UPDATABLE_FIELDS = frozenset(
    {
        "internal_user_id",
        "rights",
        "connected_at",
        "revoked_at",
        "approved_by",
        "approved_at",
        "business_account_user_id",
        "raw_payload",
    }
)


async def get_by_connection_id(
    conn: asyncpg.Connection, connection_id: str
) -> BusinessConnection | None:
    """Return the connection grant with this ``business_connection_id``, or ``None``."""
    row = await conn.fetchrow(
        "SELECT * FROM business_connections WHERE business_connection_id = $1",
        connection_id,
    )
    return BusinessConnection.from_record(row) if row is not None else None


async def create(
    conn: asyncpg.Connection, payload: BusinessConnection
) -> BusinessConnection:
    """Insert a new connection grant; server fills ``id`` / ``created_at``."""
    row = await conn.fetchrow(
        """
        INSERT INTO business_connections (
            business_connection_id, business_account_user_id, internal_user_id,
            status, rights, connected_at, revoked_at, approved_by, approved_at,
            raw_payload
        )
        VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9, $10)
        RETURNING *
        """,
        payload.business_connection_id,
        payload.business_account_user_id,
        payload.internal_user_id,
        payload.status,
        payload.rights,
        payload.connected_at,
        payload.revoked_at,
        payload.approved_by,
        payload.approved_at,
        payload.raw_payload,
    )
    assert row is not None  # INSERT ... RETURNING always yields a row
    return BusinessConnection.from_record(row)


async def update_status(
    conn: asyncpg.Connection, connection_id: str, status: str, **fields: Any
) -> None:
    """Update ``status`` (plus any whitelisted ``**fields``) for one grant.

    Example: ``update_status(conn, cid, "revoked", revoked_at=now)``. Unknown
    field names raise ``ValueError`` rather than being silently ignored.
    """
    set_parts = ["status = $2"]
    params: list[Any] = [connection_id, status]
    for key, value in fields.items():
        if key not in _UPDATABLE_FIELDS:
            raise ValueError(f"field not updatable: {key!r}")
        params.append(value)
        set_parts.append(f"{key} = ${len(params)}")
    await conn.execute(
        f"UPDATE business_connections SET {', '.join(set_parts)} "
        "WHERE business_connection_id = $1",
        *params,
    )


async def list_pending(conn: asyncpg.Connection) -> list[BusinessConnection]:
    """Return grants awaiting approval (``status = 'pending'``), oldest first."""
    rows = await conn.fetch(
        "SELECT * FROM business_connections WHERE status = 'pending' "
        "ORDER BY created_at ASC"
    )
    return [BusinessConnection.from_record(row) for row in rows]


async def list_all(conn: asyncpg.Connection) -> list[BusinessConnection]:
    """Return every connection grant, newest first."""
    rows = await conn.fetch(
        "SELECT * FROM business_connections ORDER BY created_at DESC"
    )
    return [BusinessConnection.from_record(row) for row in rows]
