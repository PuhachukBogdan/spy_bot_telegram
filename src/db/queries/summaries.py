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
            u.id   AS manager_id,
            msg.sender_name AS author_name,
            msg.sender_role AS author_role
        FROM risk_events re
        JOIN partners p ON p.id = re.partner_id
        JOIN internal_users u ON u.id = p.owner_manager_id
        LEFT JOIN messages msg ON msg.id = re.message_id
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
    share_token: str,
    expires_at: datetime,
    access_password: str,
) -> UUID:
    """Insert or upsert a summary row and return its UUID."""
    row = await conn.fetchrow(
        """
        INSERT INTO summaries (
            period_type, period_start, period_end,
            structured_content, rendered_html,
            delivery_status, share_token, expires_at, access_password
        )
        VALUES ($1, $2, $3, $4::jsonb, $5, 'pending', $6, $7, $8)
        ON CONFLICT (period_type, period_start) DO UPDATE SET
            period_end         = EXCLUDED.period_end,
            structured_content = EXCLUDED.structured_content,
            rendered_html      = EXCLUDED.rendered_html,
            share_token        = EXCLUDED.share_token,
            expires_at         = EXCLUDED.expires_at,
            access_password    = EXCLUDED.access_password,
            delivery_status    = 'pending',
            delivered_at       = NULL
        RETURNING id
        """,
        period_type,
        period_start,
        period_end,
        json.dumps({"event_count": event_count}),
        rendered_html,
        share_token,
        expires_at,
        access_password,
    )
    assert row is not None
    return UUID(str(row["id"]))


async def get_summary_by_share_token(
    conn: asyncpg.Connection,
    share_token: str,
) -> dict[str, Any] | None:
    """Return rendered_html and access_password for a valid, non-expired token."""
    row = await conn.fetchrow(
        """
        SELECT rendered_html, access_password
        FROM summaries
        WHERE share_token = $1
          AND (expires_at IS NULL OR expires_at > now())
        """,
        share_token,
    )
    if row is None:
        return None
    return dict(row)


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


async def summary_exists_since(
    conn: asyncpg.Connection,
    period_type: str,
    since: datetime,
) -> bool:
    """True if a summary of this period_type was generated at/after ``since``.

    Used by the in-process scheduler (workers.summary_scheduler_loop) to make
    firing idempotent across restarts: a row created after the scheduled instant
    means that slot was already handled, so the loop must not re-generate or
    re-post it. Keyed on ``created_at`` (not ``period_start``, which the rolling
    window varies per run).
    """
    row = await conn.fetchrow(
        """
        SELECT 1 FROM summaries
        WHERE period_type = $1 AND created_at >= $2
        LIMIT 1
        """,
        period_type,
        since,
    )
    return row is not None


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


async def get_latest_summary_html(
    conn: asyncpg.Connection,
    period_type: str,
) -> str | None:
    """Return rendered_html from the most recent non-expired summary of this type."""
    row = await conn.fetchrow(
        """
        SELECT rendered_html
        FROM summaries
        WHERE period_type = $1
          AND (expires_at IS NULL OR expires_at > now())
        ORDER BY created_at DESC
        LIMIT 1
        """,
        period_type,
    )
    if row is None or row["rendered_html"] is None:
        return None
    return str(row["rendered_html"])


async def create_dashboard(
    conn: asyncpg.Connection,
    *,
    share_token: str,
    access_password: str,
    expires_at: datetime,
) -> UUID:
    """Insert a new dashboard row and return its UUID."""
    row = await conn.fetchrow(
        """
        INSERT INTO dashboards (share_token, access_password, expires_at)
        VALUES ($1, $2, $3)
        RETURNING id
        """,
        share_token,
        access_password,
        expires_at,
    )
    assert row is not None
    return UUID(str(row["id"]))


async def get_dashboard_by_token(
    conn: asyncpg.Connection,
    share_token: str,
) -> dict[str, Any] | None:
    """Return dashboard row for a valid, non-expired token, or None."""
    row = await conn.fetchrow(
        """
        SELECT id, share_token, access_password, expires_at
        FROM dashboards
        WHERE share_token = $1
          AND (expires_at IS NULL OR expires_at > now())
        """,
        share_token,
    )
    if row is None:
        return None
    return dict(row)
