"""Alert cooldown: thread repeats of the same (chat × risk_type). Phase 11.

A single risk that keeps re-triggering (or a burst from one analysis pass) should
not spam the channel. Within ``ALERT_COOLDOWN_MINUTES`` per (chat × risk_type) we
thread repeats under the prior alert instead of posting a fresh top-level message.
Critical alerts bypass the cooldown entirely; a never-before-seen risk type has no
prior ts and naturally posts fresh.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from uuid import UUID

import asyncpg

from src.config import settings
from src.db.queries.risk_events import find_recent_alert_ts


async def resolve_thread_ts(
    conn: asyncpg.Connection,
    *,
    chat_id: UUID,
    risk_type: str,
    is_critical: bool,
) -> str | None:
    """Slack ts to thread a repeat alert under, or ``None`` to post a fresh message.

    Critical → always ``None`` (top-level ping, bypasses cooldown). Otherwise look
    for a delivered alert of the same type in the same chat within the cooldown
    window and return its ts.
    """
    if is_critical:
        return None
    since = datetime.now(UTC) - timedelta(minutes=settings.ALERT_COOLDOWN_MINUTES)
    return await find_recent_alert_ts(
        conn, chat_id=chat_id, risk_type=risk_type, since=since
    )
