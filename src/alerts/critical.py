"""Critical-alert @-mentions via critical_alert_recipients. Phase 11.

A critical risk pings named people, not just the channel. This builds the Slack
mention prefix (``<@U123> <@U456> ``) from the enabled recipients; an empty string
when nobody is configured, so a critical alert still posts (just without a ping).
"""

from __future__ import annotations

import asyncpg

from src.db.queries.alerts import list_critical_recipients


async def critical_mention_prefix(conn: asyncpg.Connection) -> str:
    """Build a trailing-spaced Slack mention prefix for critical recipients.

    Returns ``""`` when no enabled recipient has a Slack user id.
    """
    recipients = await list_critical_recipients(conn)
    mentions = [f"<@{r.slack_user_id}>" for r in recipients if r.slack_user_id]
    return " ".join(mentions) + " " if mentions else ""
