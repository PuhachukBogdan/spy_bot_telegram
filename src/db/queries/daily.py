"""DB queries for the daily digest — the first tab of the reports dashboard.

Direct aggregate SQL (no LLM — fast + cheap) over messages / risk_events / chats /
partners for a single UTC calendar-day window ``[day_start, day_end)``. Nothing is
cached or stored: every call re-aggregates, which is what lets the current-day
panel be re-fetched hourly by an open dashboard.

The digest carries risk-event counts, so it lives behind the password-gated
``/dashboard/{token}`` surface (there is no Telegram ``/daily`` command).
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime, timedelta

import asyncpg

DIGEST_MAX_AGE_DAYS = 30


def resolve_digest_day(arg: str | None, today: date) -> tuple[date | None, str | None]:
    """Resolve + validate a daily-digest day argument. Returns (day, error).

    "" / "today" → today, "yesterday" → yesterday, ``YYYY-MM-DD`` → that date.
    The default is the CURRENT day: the digest is a live view that the open
    dashboard re-fetches once an hour, so today's numbers are what a viewer
    wants by default. Rejects unparseable args, future dates, and anything
    older than 30 days. Shared by the web daily view (``?day=`` query param).
    """
    a = (arg or "").strip().lower()
    if a in ("", "today"):
        day = today
    elif a == "yesterday":
        day = today - timedelta(days=1)
    else:
        try:
            day = date.fromisoformat(a)
        except ValueError:
            return None, "Invalid date. Use YYYY-MM-DD."
    if day > today:
        return None, "That date is in the future."
    if day < today - timedelta(days=DIGEST_MAX_AGE_DAYS):
        return None, "Data older than 30 days is not available."
    return day, None


@dataclass
class DailyDigest:
    """One day's operational snapshot for the admin digest."""

    messages_total: int
    significant: int
    active_chats: int
    total_active_chats: int
    active_managers: int
    risk_low: int
    risk_medium: int
    risk_high: int
    risk_critical: int
    new_chats: int
    new_partners: int
    active_chat_rows: list[tuple[str, int]]  # (chat_name, message_count), busiest first

    @property
    def has_activity(self) -> bool:
        """True if anything at all happened that day (else the handler replies 'No activity')."""
        return bool(
            self.messages_total
            or self.new_chats
            or self.new_partners
            or self.risk_low
            or self.risk_medium
            or self.risk_high
            or self.risk_critical
        )


async def get_daily_digest(
    conn: asyncpg.Connection,
    day_start: datetime,
    day_end: datetime,
) -> DailyDigest:
    """Aggregate one UTC day's activity into a :class:`DailyDigest`.

    ``active_managers`` counts the DISTINCT humans who authorised chats that saw
    traffic that day (``chats.authorized_by`` → the real managing people), NOT the
    per-affiliate ``internal_users`` role=manager stubs.
    """
    messages_total = (
        await conn.fetchval(
            "SELECT COUNT(*)::int FROM messages WHERE created_at >= $1 AND created_at < $2",
            day_start,
            day_end,
        )
    ) or 0
    significant = (
        await conn.fetchval(
            "SELECT COUNT(*)::int FROM messages "
            "WHERE created_at >= $1 AND created_at < $2 AND is_significant = true",
            day_start,
            day_end,
        )
    ) or 0
    total_active_chats = (
        await conn.fetchval("SELECT COUNT(*)::int FROM chats WHERE status = 'active'")
    ) or 0
    active_managers = (
        await conn.fetchval(
            """
            SELECT COUNT(DISTINCT c.authorized_by)::int
            FROM messages m
            JOIN chats c ON c.id = m.chat_id
            JOIN internal_users u ON u.id = c.authorized_by AND u.role = 'manager'
            WHERE m.created_at >= $1 AND m.created_at < $2
            """,
            day_start,
            day_end,
        )
    ) or 0

    risk_rows = await conn.fetch(
        """
        SELECT risk_level, COUNT(*)::int AS c
        FROM risk_events
        WHERE created_at >= $1 AND created_at < $2
        GROUP BY risk_level
        """,
        day_start,
        day_end,
    )
    rl = {str(r["risk_level"]): int(r["c"]) for r in risk_rows}

    new_chats = (
        await conn.fetchval(
            "SELECT COUNT(*)::int FROM chats "
            "WHERE status = 'active' AND authorized_at >= $1 AND authorized_at < $2",
            day_start,
            day_end,
        )
    ) or 0
    new_partners = (
        await conn.fetchval(
            "SELECT COUNT(*)::int FROM partners WHERE created_at >= $1 AND created_at < $2",
            day_start,
            day_end,
        )
    ) or 0

    # Every chat with at least one message that day (busiest first).
    active_rows = await conn.fetch(
        """
        SELECT c.id, c.chat_name, COUNT(*)::int AS n
        FROM messages m
        JOIN chats c ON c.id = m.chat_id
        WHERE m.created_at >= $1 AND m.created_at < $2
        GROUP BY c.id, c.chat_name
        ORDER BY n DESC, c.chat_name
        """,
        day_start,
        day_end,
    )
    active_chat_rows = [
        (str(r["chat_name"] or "(untitled)"), int(r["n"])) for r in active_rows
    ]

    return DailyDigest(
        messages_total=int(messages_total),
        significant=int(significant),
        active_chats=len(active_chat_rows),
        total_active_chats=int(total_active_chats),
        active_managers=int(active_managers),
        risk_low=rl.get("low", 0),
        risk_medium=rl.get("medium", 0),
        risk_high=rl.get("high", 0),
        risk_critical=rl.get("critical", 0),
        new_chats=int(new_chats),
        new_partners=int(new_partners),
        active_chat_rows=active_chat_rows,
    )
