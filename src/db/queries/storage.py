"""Database storage-size query. Storage-monitoring worker (Supabase size cap).

A single read-only aggregate: the on-disk size of the current database, used to
warn admins before the Supabase plan's size cap is hit. Takes an already-acquired
``asyncpg.Connection`` like every other query module.
"""

from __future__ import annotations

from typing import cast

import asyncpg


async def get_database_size_bytes(conn: asyncpg.Connection) -> int:
    """Return the on-disk size of the current database in bytes.

    ``pg_database_size`` is a cheap catalog lookup available to the pooled
    ``postgres.<ref>`` role, so it works over the Supabase session pooler.
    """
    value = await conn.fetchval("SELECT pg_database_size(current_database())")
    return cast("int", value)
