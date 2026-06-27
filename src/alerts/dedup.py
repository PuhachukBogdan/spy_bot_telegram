"""Risk-case resolution: one open case is one Slack card. Phase 11 + case rework.

A risk that keeps developing across analysis passes (an opening line, the
follow-ups, the agreement that confirms it) is ONE case, not N alerts. Within
``RISK_CASE_WINDOW_MINUTES`` per (chat × risk_type) a new alertable finding belongs
to the same open case: the dispatcher updates that case's existing card in place
instead of posting a fresh top-level alert. Critical findings no longer bypass this
— a critical that escalates an open case updates the card (and re-pings) rather
than spamming a second message. A risk type with no open case in the window opens a
fresh card naturally (no prior ts).
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from uuid import UUID

import asyncpg

from src.config import settings
from src.db.queries.risk_events import find_recent_alert_ts


async def resolve_open_case_ts(
    conn: asyncpg.Connection, *, chat_id: UUID, risk_type: str
) -> str | None:
    """Slack ts of the open case for (chat, risk_type), or ``None`` to open fresh.

    Looks for a delivered alert of the same type in the same chat within
    ``RISK_CASE_WINDOW_MINUTES``. Its ts is the card the new finding should update;
    ``None`` means no open case — post a fresh top-level alert.
    """
    since = datetime.now(UTC) - timedelta(minutes=settings.RISK_CASE_WINDOW_MINUTES)
    return await find_recent_alert_ts(
        conn, chat_id=chat_id, risk_type=risk_type, since=since
    )
