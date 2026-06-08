"""Alert-domain queries: critical recipients + failed-alert log. Phase 11.

Small plain-SQL helpers over ``critical_alert_recipients`` (who to @-ping on a
critical) and ``failed_alerts`` (alerts that never reached Slack). Each takes an
already-acquired ``asyncpg.Connection`` (project-wide convention).
"""

from __future__ import annotations

from typing import Any
from uuid import UUID

import asyncpg

from src.db.models import CriticalAlertRecipient


async def list_critical_recipients(
    conn: asyncpg.Connection,
) -> list[CriticalAlertRecipient]:
    """Enabled critical-alert recipients that carry a Slack user id (for @mentions).

    Rows without a ``slack_user_id`` can't be pinged in Slack, so they're filtered
    out here rather than producing an empty ``<@>`` mention.
    """
    rows = await conn.fetch(
        """
        SELECT * FROM critical_alert_recipients
        WHERE enabled = true AND slack_user_id IS NOT NULL
        ORDER BY full_name ASC
        """
    )
    return [CriticalAlertRecipient.from_record(row) for row in rows]


async def record_failed_alert(
    conn: asyncpg.Connection,
    *,
    risk_event_id: UUID | None,
    channel: str,
    payload: dict[str, Any] | None,
    error: str,
) -> None:
    """Persist an alert that could not be delivered (for inspection / later retry).

    ``payload`` rides the pool's jsonb codec. ``last_attempt_at`` is stamped now;
    ``resolved`` defaults false. This is a best-effort breadcrumb — the caller
    (``src.alerts.failed``) swallows any failure here so logging a failed alert can
    never itself crash the worker.
    """
    await conn.execute(
        """
        INSERT INTO failed_alerts (risk_event_id, channel, payload, error, last_attempt_at)
        VALUES ($1, $2, $3, $4, now())
        """,
        risk_event_id,
        channel,
        payload,
        error,
    )
