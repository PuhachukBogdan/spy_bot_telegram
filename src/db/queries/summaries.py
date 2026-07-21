"""DB queries for weekly/monthly HTML summary reports. Phase 16."""

from __future__ import annotations

import json
from datetime import datetime
from typing import Any
from uuid import UUID

import asyncpg


async def list_active_chats(conn: asyncpg.Connection) -> list[dict[str, Any]]:
    """All active monitored chat units, including those with zero risk events.

    Each row is one monitored unit (group, forum topic, or business chat) from the
    ``chats`` table with ``status='active'`` — this is what the report is keyed on.
    ``manager_name`` is the human who authorised the chat
    (``chats.authorized_by`` → ``internal_users.full_name``), falling back to
    ``'unassigned'`` when that link is missing.

    NOTE: the report is NOT keyed on ``internal_users`` role=manager rows — those
    are one-stub-per-affiliate (keyed on aff_id from the chat title), not the real
    managing humans. The managing human surfaces here via ``manager_name`` instead.
    """
    rows = await conn.fetch(
        """
        SELECT
            c.id,
            c.chat_name,
            c.topic_name,
            c.is_test,
            COALESCE(NULLIF(btrim(u.full_name), ''), 'unassigned') AS manager_name,
            COALESCE(u.is_test, false) AS manager_is_test
        FROM chats c
        -- Only resolve the authoriser as a MANAGER; admins (e.g. the ops admin who
        -- authorised a couple of chats) are NOT managers → they fall to 'unassigned'
        -- and never appear in the manager column or the report's manager filter.
        LEFT JOIN internal_users u
               ON u.id = c.authorized_by AND u.role = 'manager'
        WHERE c.status = 'active'
        ORDER BY c.chat_name, c.topic_name
        """
    )
    return [dict(r) for r in rows]


async def count_proposals_by_chat(
    conn: asyncpg.Connection,
    since: datetime,
    until: datetime,
) -> dict[str, int]:
    """Manager-proposal counts per chat_id within the period.

    ``activity_signals`` rows with ``signal_type='manager_proposal'`` grouped by
    chat. Returned as {chat_id_str: count} so the builder can badge each chat with
    how many proposals its manager made in the reporting window.
    """
    rows = await conn.fetch(
        """
        SELECT chat_id, COUNT(*)::int AS c
        FROM activity_signals
        WHERE signal_type = 'manager_proposal'
          AND chat_id IS NOT NULL
          AND created_at >= $1 AND created_at < $2
        GROUP BY chat_id
        """,
        since,
        until,
    )
    return {str(r["chat_id"]): int(r["c"]) for r in rows}


async def count_chats_added(
    conn: asyncpg.Connection,
    since: datetime,
    until: datetime,
) -> int:
    """Number of chats onboarded (authorised) within the report window.

    Counts ``chats`` rows that are currently ``status='active'`` whose
    ``authorized_at`` falls in [since, until). Surfaced as the "New chats" stat
    (weekly = last 7 days, monthly = last 30 days).
    """
    val = await conn.fetchval(
        """
        SELECT COUNT(*)::int
        FROM chats
        WHERE status = 'active'
          AND authorized_at >= $1 AND authorized_at < $2
        """,
        since,
        until,
    )
    return int(val or 0)


async def list_chat_added_dates(
    conn: asyncpg.Connection,
    since: datetime,
    until: datetime,
) -> list[datetime]:
    """authorized_at timestamps of chats onboarded in the window (monthly filter).

    Lets the client-side monthly date-range picker recompute the "New chats" count
    for a sub-range, the same way proposal dates drive the proposals count.
    """
    rows = await conn.fetch(
        """
        SELECT authorized_at
        FROM chats
        WHERE status = 'active'
          AND authorized_at >= $1 AND authorized_at < $2
        ORDER BY authorized_at
        """,
        since,
        until,
    )
    return [r["authorized_at"] for r in rows]


async def list_events_by_chat(
    conn: asyncpg.Connection,
    since: datetime,
    until: datetime,
) -> list[dict[str, Any]]:
    """All risk events in the period, attributed to their monitored chat unit.

    Ordered by (chat, event time) for the per-chat timeline sections. Uses the
    direct ``risk_events.chat_id`` link (every event carries one), so events whose
    ``partner_id`` is NULL — which the old partner→owner_manager JOIN silently
    dropped — are still included. ``partner_name`` prefers the linked partner's
    name, falling back to the chat title. Events dismissed via Slack
    (status='false_positive') are excluded, as before.
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
            re.chat_id,
            COALESCE(p.name, c.chat_name) AS partner_name,
            msg.sender_name AS author_name,
            msg.sender_role AS author_role
        FROM risk_events re
        JOIN chats c ON c.id = re.chat_id
        LEFT JOIN partners p ON p.id = re.partner_id
        LEFT JOIN messages msg ON msg.id = re.message_id
        WHERE re.created_at >= $1 AND re.created_at < $2
          AND c.status = 'active'
          -- events dismissed via Slack (False Positive / Suppress both write
          -- status='false_positive') never appear in weekly/monthly reports
          AND re.status IS DISTINCT FROM 'false_positive'
        ORDER BY re.chat_id, re.created_at
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


async def dashboard_token_known(
    conn: asyncpg.Connection,
    share_token: str,
) -> bool:
    """True if the token exists at all (even if expired/revoked).

    Lets the route tell "superseded by a newer report" (show a friendly notice)
    from "never existed" (a plain 404).
    """
    row = await conn.fetchrow(
        "SELECT 1 FROM dashboards WHERE share_token = $1", share_token
    )
    return row is not None


async def get_active_dashboard(
    conn: asyncpg.Connection,
) -> dict[str, Any] | None:
    """The currently-advertised dashboard: newest non-expired row with a Slack ts.

    At steady state there is exactly one (each new report revokes the rest). Used
    to find the previous Slack message so it can be retired when a newer report
    is posted.
    """
    row = await conn.fetchrow(
        """
        SELECT id, share_token, slack_channel, slack_ts
        FROM dashboards
        WHERE slack_ts IS NOT NULL
          AND (expires_at IS NULL OR expires_at > now())
        ORDER BY created_at DESC
        LIMIT 1
        """
    )
    if row is None:
        return None
    return dict(row)


async def set_dashboard_slack(
    conn: asyncpg.Connection,
    dashboard_id: UUID,
    slack_channel: str,
    slack_ts: str,
) -> None:
    """Record the Slack message (channel + ts) that advertises this dashboard."""
    await conn.execute(
        "UPDATE dashboards SET slack_channel = $2, slack_ts = $3 WHERE id = $1",
        dashboard_id,
        slack_channel,
        slack_ts,
    )


async def revoke_dashboards_except(
    conn: asyncpg.Connection,
    keep_id: UUID,
) -> int:
    """Expire every dashboard token except ``keep_id`` (revoke old Slack links).

    Sets ``expires_at = now()`` so :func:`get_dashboard_by_token` stops resolving
    them. Only touches links still live. Returns the number revoked.
    """
    result = await conn.execute(
        """
        UPDATE dashboards
        SET expires_at = now()
        WHERE id <> $1
          AND (expires_at IS NULL OR expires_at > now())
        """,
        keep_id,
    )
    # asyncpg returns e.g. "UPDATE 3"
    try:
        return int(result.split()[-1])
    except (ValueError, IndexError):
        return 0
