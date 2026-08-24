"""Phase 2 metric queries. Read-only; nothing here writes.

Two windowing notes that apply to every query in this module:

* The window keys on ``messages.timestamp`` (when it was SENT), not ``created_at``
  (when we ingested it). These metrics describe a conversation, so the
  conversation's own clock is the right one — a message delayed in the queue must
  not read as a slow reply.
* Everything filters ``source <> 'imported'``. Written as an exclusion, never as
  an allow-list on ``'live'``: the real values are ``live_group``, ``business``
  and a bare ``live`` on six pre-0006 rows, so an allow-list would silently drop
  almost every genuine message.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any
from uuid import UUID

import asyncpg


async def list_sla_messages(
    conn: asyncpg.Connection, since: datetime, until: datetime
) -> list[dict[str, Any]]:
    """Messages in owned, active group/topic chats, ordered for SLA pairing.

    Business (private) units are excluded: the SLA track is about partner groups.
    Chats with no ``authorized_by`` are excluded too — with nobody to attribute a
    reply to there is no metric, and guessing an owner would put another manager's
    silence on someone's record.

    Returned in ``(chat, time)`` order so the caller can walk each conversation
    once and pair waits to replies without sorting again.
    """
    rows = await conn.fetch(
        """
        SELECT m.chat_id,
               m.timestamp,
               m.sender_role,
               m.sender_id,
               COALESCE(char_length(m.message_text), 0) AS chars,
               c.authorized_by AS manager_id
        FROM messages m
        JOIN chats c ON c.id = m.chat_id
        WHERE m.timestamp >= $1
          AND m.timestamp < $2
          AND m.source <> 'imported'
          AND c.status = 'active'
          AND COALESCE(c.is_test, false) = false
          AND c.unit_type IN ('group', 'topic')
          AND c.authorized_by IS NOT NULL
        ORDER BY m.chat_id, m.timestamp
        """,
        since,
        until,
    )
    return [dict(r) for r in rows]


async def count_messages_per_chat(
    conn: asyncpg.Connection, since: datetime, until: datetime
) -> list[dict[str, Any]]:
    """One row per owned active chat with its message count in the window.

    A LEFT JOIN, so a chat with zero traffic still appears — that is the whole
    point of the KPI: silent chats are the denominator, and an INNER JOIN would
    quietly make every manager look 100% active.
    """
    rows = await conn.fetch(
        """
        SELECT c.authorized_by AS manager_id,
               c.id            AS chat_id,
               c.chat_name,
               c.topic_name,
               c.unit_type,
               c.created_at,
               COUNT(m.id)     AS messages
        FROM chats c
        LEFT JOIN messages m
               ON m.chat_id = c.id
              AND m.timestamp >= $1
              AND m.timestamp < $2
              AND m.source <> 'imported'
        WHERE c.status = 'active'
          AND COALESCE(c.is_test, false) = false
          AND c.authorized_by IS NOT NULL
        GROUP BY c.authorized_by, c.id, c.chat_name, c.topic_name, c.unit_type,
                 c.created_at
        """,
        since,
        until,
    )
    return [dict(r) for r in rows]


async def count_messages_per_chat_day(
    conn: asyncpg.Connection, since: datetime, until: datetime, tz: str
) -> list[dict[str, Any]]:
    """(manager, chat, local day) -> message count, for coverage trend buckets.

    Only days that actually had messages appear; the zero days are implied by the
    chat registry (:func:`count_messages_per_chat` carries every owned chat plus
    its ``created_at``, which bounds the denominator per bucket). Days are local
    to ``tz`` so buckets line up with the rest of the reporting calendar.
    """
    rows = await conn.fetch(
        """
        SELECT c.authorized_by AS manager_id,
               c.id            AS chat_id,
               (m.timestamp AT TIME ZONE $3)::date AS day,
               COUNT(m.id)     AS messages
        FROM messages m
        JOIN chats c ON c.id = m.chat_id
        WHERE m.timestamp >= $1
          AND m.timestamp < $2
          AND m.source <> 'imported'
          AND c.status = 'active'
          AND COALESCE(c.is_test, false) = false
          AND c.authorized_by IS NOT NULL
        GROUP BY c.authorized_by, c.id, day
        """,
        since,
        until,
        tz,
    )
    return [dict(r) for r in rows]


async def count_proposals_per_day(
    conn: asyncpg.Connection, since: datetime, until: datetime, tz: str
) -> list[dict[str, Any]]:
    """(manager, local day) -> manager_proposal count, for the proposals trend."""
    rows = await conn.fetch(
        """
        SELECT u.id AS manager_id,
               (s.created_at AT TIME ZONE $3)::date AS day,
               COUNT(*) AS proposals
        FROM activity_signals s
        JOIN internal_users u
          ON u.telegram_accounts @> to_jsonb(s.sender_id)
        WHERE s.signal_type = 'manager_proposal'
          AND s.created_at >= $1
          AND s.created_at < $2
        GROUP BY u.id, day
        """,
        since,
        until,
        tz,
    )
    return [dict(r) for r in rows]


async def list_risk_days(
    conn: asyncpg.Connection, since: datetime, until: datetime, tz: str
) -> list[dict[str, Any]]:
    """One light row per risk event for the risk trend: owner, author, local day.

    Attribution (did the OWNING manager write it?) happens in Python against the
    manager index, same as the dossier — so the trend and the dossier can never
    disagree about whose conduct a case was.
    """
    rows = await conn.fetch(
        """
        SELECT c.authorized_by AS manager_id,
               r.sender_id,
               (r.created_at AT TIME ZONE $3)::date AS day
        FROM risk_events r
        JOIN chats c ON c.id = r.chat_id
        WHERE r.created_at >= $1
          AND r.created_at < $2
          AND r.status IS DISTINCT FROM 'false_positive'
          AND c.status = 'active'
          AND COALESCE(c.is_test, false) = false
          AND c.authorized_by IS NOT NULL
        """,
        since,
        until,
        tz,
    )
    return [dict(r) for r in rows]


async def list_risk_events(
    conn: asyncpg.Connection, since: datetime, until: datetime
) -> list[dict[str, Any]]:
    """Risk events in the window, with the chat and its owning manager.

    Windowed on ``created_at`` (DETECTION time), unlike the message queries which
    use send time: a risk case belongs to the period in which it was found, which
    is also how the live weekly/monthly report keys it. The two clocks differ by
    up to an analysis cycle, and mixing them would put an event in one period on
    one surface and another period elsewhere.

    Dismissed events are dropped (``status <> 'false_positive'``), matching the
    live report — a case a human already rejected should not resurface as a
    number on someone's record.
    """
    rows = await conn.fetch(
        """
        SELECT r.id,
               r.chat_id,
               r.risk_type,
               r.risk_level,
               r.final_score,
               r.created_at,
               r.detected_phrase,
               r.llm_explanation,
               r.sender_id,
               r.status,
               c.chat_name,
               c.topic_name,
               c.unit_type,
               c.authorized_by AS manager_id
        FROM risk_events r
        JOIN chats c ON c.id = r.chat_id
        WHERE r.created_at >= $1
          AND r.created_at < $2
          AND r.status IS DISTINCT FROM 'false_positive'
          AND c.status = 'active'
          AND COALESCE(c.is_test, false) = false
          AND c.authorized_by IS NOT NULL
        ORDER BY r.created_at DESC
        """,
        since,
        until,
    )
    return [dict(r) for r in rows]


async def count_proposals_by_manager(
    conn: asyncpg.Connection, since: datetime, until: datetime
) -> dict[UUID, int]:
    """``manager_id -> manager_proposal count`` in the window.

    Attribution is by the AUTHOR of the signal (``activity_signals.sender_id``
    matched against ``telegram_accounts``), not by chat ownership: a proposal is
    something a person did, so it belongs to whoever made it.
    """
    rows = await conn.fetch(
        """
        SELECT u.id AS manager_id, COUNT(*) AS proposals
        FROM activity_signals s
        JOIN internal_users u
          ON u.telegram_accounts @> to_jsonb(s.sender_id)
        WHERE s.signal_type = 'manager_proposal'
          AND s.created_at >= $1
          AND s.created_at < $2
        GROUP BY u.id
        """,
        since,
        until,
    )
    return {r["manager_id"]: r["proposals"] for r in rows}
