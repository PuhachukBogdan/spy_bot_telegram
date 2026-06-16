"""DB queries for weekly/monthly HTML summary reports. Phase 16."""

from __future__ import annotations

import json
from datetime import datetime
from typing import Any
from uuid import UUID

import asyncpg


async def list_active_managers(conn: asyncpg.Connection) -> list[dict[str, Any]]:
    """All enabled managers ordered by name, including those with zero events."""
    rows = await conn.fetch(
        """
        SELECT id, full_name, aff_id, tg_username
        FROM internal_users
        WHERE role = 'manager' AND enabled = true
        ORDER BY full_name
        """
    )
    return [dict(r) for r in rows]


async def risk_heatmap(
    conn: asyncpg.Connection,
    since: datetime,
    until: datetime,
) -> list[dict[str, Any]]:
    """Count of risk events per (manager_id, risk_type) for the period.

    Returns rows: manager_id UUID, risk_type TEXT, cnt INT.
    """
    rows = await conn.fetch(
        """
        SELECT
            u.id          AS manager_id,
            re.risk_type,
            COUNT(*)::int AS cnt
        FROM risk_events re
        JOIN partners p ON p.id = re.partner_id
        JOIN internal_users u ON u.id = p.owner_manager_id
        WHERE re.created_at >= $1 AND re.created_at < $2
          AND u.role = 'manager' AND u.enabled = true
        GROUP BY u.id, re.risk_type
        """,
        since,
        until,
    )
    return [dict(r) for r in rows]


async def list_events_for_report(
    conn: asyncpg.Connection,
    since: datetime,
    until: datetime,
) -> list[dict[str, Any]]:
    """All risk events in the period with manager and partner attribution.

    Ordered by (manager, event time) for the per-manager timeline sections.
    """
    rows = await conn.fetch(
        """
        SELECT
            re.id,
            re.risk_type,
            re.risk_level,
            re.final_score,
            re.detected_phrase,
            re.llm_explanation,
            re.status,
            re.created_at,
            p.name AS partner_name,
            u.id   AS manager_id
        FROM risk_events re
        JOIN partners p ON p.id = re.partner_id
        JOIN internal_users u ON u.id = p.owner_manager_id
        WHERE re.created_at >= $1 AND re.created_at < $2
          AND u.role = 'manager' AND u.enabled = true
        ORDER BY u.id, re.created_at
        """,
        since,
        until,
    )
    return [dict(r) for r in rows]


async def save_summary(
    conn: asyncpg.Connection,
    *,
    period_type: str,
    period_start: datetime,
    period_end: datetime,
    rendered_html: str,
    event_count: int,
) -> UUID:
    """Insert a new summary row and return its UUID."""
    row = await conn.fetchrow(
        """
        INSERT INTO summaries (
            period_type, period_start, period_end,
            structured_content, rendered_html,
            delivery_status
        )
        VALUES ($1, $2, $3, $4::jsonb, $5, 'pending')
        RETURNING id
        """,
        period_type,
        period_start,
        period_end,
        json.dumps({"event_count": event_count}),
        rendered_html,
    )
    assert row is not None
    return UUID(str(row["id"]))


async def mark_summary_delivered(
    conn: asyncpg.Connection,
    summary_id: UUID,
) -> None:
    """Stamp delivery_status=delivered and delivered_at=now()."""
    await conn.execute(
        """
        UPDATE summaries
        SET delivery_status = 'delivered', delivered_at = now()
        WHERE id = $1
        """,
        summary_id,
    )


async def get_rendered_html(
    conn: asyncpg.Connection,
    period_type: str,
    period_start: datetime,
) -> str | None:
    """Return the most recently generated HTML for a given period, or None."""
    row = await conn.fetchrow(
        """
        SELECT rendered_html
        FROM summaries
        WHERE period_type = $1 AND period_start = $2
        ORDER BY created_at DESC
        LIMIT 1
        """,
        period_type,
        period_start,
    )
    if row is None or row["rendered_html"] is None:
        return None
    return str(row["rendered_html"])
