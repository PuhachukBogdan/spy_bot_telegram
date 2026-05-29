"""red_flag_patterns queries. Phase 6.

Loads the Tier-1 dictionary and a cheap fingerprint used for hot-reload (the
matcher keeps patterns in memory and only recompiles when the fingerprint
changes). Takes an already-acquired ``asyncpg.Connection``.
"""

from __future__ import annotations

from datetime import datetime

import asyncpg

from src.db.models import RedFlagPattern


async def load_enabled_patterns(conn: asyncpg.Connection) -> list[RedFlagPattern]:
    """Return all enabled patterns (the active Tier-1 dictionary)."""
    rows = await conn.fetch(
        "SELECT * FROM red_flag_patterns WHERE enabled = true ORDER BY id"
    )
    return [RedFlagPattern.from_record(row) for row in rows]


async def patterns_fingerprint(conn: asyncpg.Connection) -> tuple[int, datetime | None]:
    """Return ``(enabled_count, max_updated_at)`` to detect dictionary changes.

    A new/removed/toggled pattern changes the count; an edit bumps ``updated_at``.
    Comparing this tuple lets the in-memory cache skip recompilation when nothing
    changed (CLAUDE.md Phase 6: hot-reload every 5 min).
    """
    row = await conn.fetchrow(
        """
        SELECT count(*) AS n, max(updated_at) AS latest
        FROM red_flag_patterns
        WHERE enabled = true
        """
    )
    assert row is not None  # aggregate always returns one row
    return int(row["n"]), row["latest"]
