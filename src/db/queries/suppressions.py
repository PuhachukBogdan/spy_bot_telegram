"""Queries over ``suppression_rules`` (migration 0020).

A staff-managed allowlist that gates the alert layer: an alertable risk_event
matching an active rule is not posted to Slack (the risk_event itself is still
persisted). Plain SQL over an already-acquired connection (project convention).
"""

from __future__ import annotations

import asyncpg

from src.db.models import SuppressionRule


async def list_active_suppressions(conn: asyncpg.Connection) -> list[SuppressionRule]:
    """All active suppression rules, newest first."""
    rows = await conn.fetch(
        "SELECT * FROM suppression_rules WHERE active = true ORDER BY created_at DESC"
    )
    return [SuppressionRule.from_record(row) for row in rows]


async def create_suppression(
    conn: asyncpg.Connection,
    *,
    risk_type: str | None,
    pattern: str,
    note: str | None,
    created_by: str | None,
) -> None:
    """Insert a suppression rule (created by the Slack '🔕 Suppress' button)."""
    await conn.execute(
        """
        INSERT INTO suppression_rules (risk_type, pattern, note, created_by)
        VALUES ($1, $2, $3, $4)
        """,
        risk_type,
        pattern,
        note,
        created_by,
    )
