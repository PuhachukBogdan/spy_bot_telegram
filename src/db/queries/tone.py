"""Tone-of-voice tables (migration 0025): targets, persistence, page reads.

Three tables, three jobs:

* ``manager_tone_daily`` — additive counters per (manager, local day, metric):
  ``flagged`` and ``assessed``. NOT tied to ``messages``, so the numbers survive
  the 120-day retention purge and the page keeps its history.
* ``manager_tone_flags`` — one row per accepted flag, with the verbatim quote, for
  the dossier's folded review list. FK onto ``messages`` with CASCADE: when the
  purge drops a message its flag goes with it; the counter above does not.
* ``manager_tone_progress`` — one row per completed (chat, day): makes the daily
  pass idempotent and resumable, and keeps per-call cost accounting.

Every messages-derived read excludes ``source = 'imported'`` and non-active or
test chats, same as the other Phase 2 queries (CLAUDE.md §17 trap 1).
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime
from decimal import Decimal
from typing import Any
from uuid import UUID

import asyncpg

from src.db.models import Message


async def list_tone_targets(
    conn: asyncpg.Connection,
    since: datetime,
    until: datetime,
    tz: str,
    manager_telegram_ids: list[int],
) -> list[dict[str, Any]]:
    """(chat, local day) pairs that hold at least one real-manager text message.

    The pass judges only what managers wrote, so a chat-day with partner traffic
    alone costs nothing — it is not a target. Business (private) units are out,
    like every other Phase 2 metric; topics and groups are in.
    """
    rows = await conn.fetch(
        """
        SELECT m.chat_id,
               (m.timestamp AT TIME ZONE $3)::date AS day,
               COUNT(*) AS messages,
               COUNT(*) FILTER (
                   WHERE m.sender_id = ANY($4::bigint[])
                     AND COALESCE(NULLIF(m.message_text, ''), m.transcription) IS NOT NULL
               ) AS manager_messages
        FROM messages m
        JOIN chats c ON c.id = m.chat_id
        WHERE m.timestamp >= $1
          AND m.timestamp < $2
          AND m.source <> 'imported'
          AND c.status = 'active'
          AND COALESCE(c.is_test, false) = false
          AND c.unit_type IN ('group', 'topic')
        GROUP BY m.chat_id, day
        HAVING COUNT(*) FILTER (
                   WHERE m.sender_id = ANY($4::bigint[])
                     AND COALESCE(NULLIF(m.message_text, ''), m.transcription) IS NOT NULL
               ) > 0
        ORDER BY day, m.chat_id
        """,
        since,
        until,
        tz,
        manager_telegram_ids,
    )
    return [dict(r) for r in rows]


async def list_done_chat_days(
    conn: asyncpg.Connection, since_day: date, until_day: date
) -> set[tuple[UUID, date]]:
    """(chat, day) pairs already processed inside ``[since_day, until_day]``."""
    rows = await conn.fetch(
        """
        SELECT chat_id, day
        FROM manager_tone_progress
        WHERE day >= $1 AND day <= $2
        """,
        since_day,
        until_day,
    )
    return {(r["chat_id"], r["day"]) for r in rows}


async def load_chat_day_messages(
    conn: asyncpg.Connection,
    chat_id: UUID,
    start: datetime,
    end: datetime,
    *,
    context_limit: int,
) -> tuple[list[Message], list[Message]]:
    """``(context, day_messages)`` for one chat: the last ``context_limit``
    messages before ``start`` (read-only lead-in) and everything in ``[start, end)``.
    """
    ctx_rows = await conn.fetch(
        """
        SELECT * FROM messages
        WHERE chat_id = $1
          AND timestamp < $2
          AND source <> 'imported'
        ORDER BY timestamp DESC
        LIMIT $3
        """,
        chat_id,
        start,
        context_limit,
    )
    day_rows = await conn.fetch(
        """
        SELECT * FROM messages
        WHERE chat_id = $1
          AND timestamp >= $2
          AND timestamp < $3
          AND source <> 'imported'
        ORDER BY timestamp
        """,
        chat_id,
        start,
        end,
    )
    context = [Message.from_record(r) for r in reversed(ctx_rows)]
    day_messages = [Message.from_record(r) for r in day_rows]
    return context, day_messages


@dataclass(frozen=True)
class ToneFlagRow:
    """One accepted flag, ready to insert."""

    manager_id: UUID
    chat_id: UUID
    message_id: UUID
    metric: str
    confidence: float
    quote: str
    reason: str
    occurred_at: datetime
    day: date
    sender_name: str | None


async def persist_chat_day(
    conn: asyncpg.Connection,
    *,
    chat_id: UUID,
    day: date,
    messages: int,
    assessed_by_manager: dict[UUID, int],
    flagged: dict[tuple[UUID, str], int],
    metric_keys: list[str],
    flags: list[ToneFlagRow],
    model: str,
    prompt_version: str,
    tokens_in: int,
    tokens_out: int,
    cost_usd: Decimal,
) -> None:
    """Write one completed chat-day atomically: progress + counters + flags.

    One transaction, so a crash can never leave counters bumped without the
    progress row (which would double-count on the retry) or the reverse. Every
    manager who wrote that day gets a counter row for EVERY metric, flagged or
    not — the zero rows are the denominator.
    """
    async with conn.transaction():
        await conn.execute(
            """
            INSERT INTO manager_tone_progress (
                chat_id, day, messages, assessed, flags,
                input_tokens, output_tokens, cost_usd, model, prompt_version
            )
            VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9, $10)
            ON CONFLICT (chat_id, day) DO NOTHING
            """,
            chat_id,
            day,
            messages,
            sum(assessed_by_manager.values()),
            len(flags),
            tokens_in,
            tokens_out,
            cost_usd,
            model,
            prompt_version,
        )
        for manager_id, assessed in assessed_by_manager.items():
            for metric in metric_keys:
                await conn.execute(
                    """
                    INSERT INTO manager_tone_daily (manager_id, day, metric, flagged, assessed)
                    VALUES ($1, $2, $3, $4, $5)
                    ON CONFLICT (manager_id, day, metric) DO UPDATE
                    SET flagged    = manager_tone_daily.flagged  + EXCLUDED.flagged,
                        assessed   = manager_tone_daily.assessed + EXCLUDED.assessed,
                        updated_at = now()
                    """,
                    manager_id,
                    day,
                    metric,
                    flagged.get((manager_id, metric), 0),
                    assessed,
                )
        for f in flags:
            await conn.execute(
                """
                INSERT INTO manager_tone_flags (
                    manager_id, chat_id, message_id, metric, confidence, quote, reason,
                    occurred_at, day, sender_name, model, prompt_version
                )
                VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9, $10, $11, $12)
                ON CONFLICT (message_id, metric) DO NOTHING
                """,
                f.manager_id,
                f.chat_id,
                f.message_id,
                f.metric,
                f.confidence,
                f.quote,
                f.reason,
                f.occurred_at,
                f.day,
                f.sender_name,
                model,
                prompt_version,
            )


async def tone_spend_since(conn: asyncpg.Connection, since: datetime) -> Decimal:
    """Reported spend of the tone pass since ``since`` (the per-day ceiling's clock)."""
    value = await conn.fetchval(
        "SELECT COALESCE(SUM(cost_usd), 0) FROM manager_tone_progress WHERE created_at >= $1",
        since,
    )
    return Decimal(str(value or 0))


async def list_tone_days(
    conn: asyncpg.Connection, since_day: date, until_day: date
) -> list[dict[str, Any]]:
    """Counter rows for the page: (manager, day, metric, flagged, assessed)."""
    rows = await conn.fetch(
        """
        SELECT manager_id, day, metric, flagged, assessed
        FROM manager_tone_daily
        WHERE day >= $1 AND day <= $2
        ORDER BY manager_id, day, metric
        """,
        since_day,
        until_day,
    )
    return [dict(r) for r in rows]


async def list_tone_flags(
    conn: asyncpg.Connection,
    since_day: date,
    until_day: date,
    *,
    limit: int,
) -> list[dict[str, Any]]:
    """Accepted flags for the dossier's review list, newest first, with chat labels."""
    rows = await conn.fetch(
        """
        SELECT f.id, f.manager_id, f.chat_id, f.message_id, f.metric, f.confidence,
               f.quote, f.reason, f.occurred_at, f.day,
               c.chat_name, c.unit_type
        FROM manager_tone_flags f
        JOIN chats c ON c.id = f.chat_id
        WHERE f.day >= $1 AND f.day <= $2
        ORDER BY f.occurred_at DESC
        LIMIT $3
        """,
        since_day,
        until_day,
        limit,
    )
    return [dict(r) for r in rows]


async def tone_summary(
    conn: asyncpg.Connection, since_day: date, until_day: date
) -> list[dict[str, Any]]:
    """Per-manager, per-metric totals for the CLI status view."""
    rows = await conn.fetch(
        """
        SELECT u.full_name, d.manager_id, d.metric,
               SUM(d.flagged)  AS flagged,
               MAX(a.assessed) AS assessed
        FROM manager_tone_daily d
        JOIN internal_users u ON u.id = d.manager_id
        JOIN (
            SELECT manager_id, SUM(assessed) AS assessed
            FROM (
                SELECT manager_id, day, MAX(assessed) AS assessed
                FROM manager_tone_daily
                WHERE day >= $1 AND day <= $2
                GROUP BY manager_id, day
            ) per_day
            GROUP BY manager_id
        ) a ON a.manager_id = d.manager_id
        WHERE d.day >= $1 AND d.day <= $2
        GROUP BY u.full_name, d.manager_id, d.metric
        ORDER BY u.full_name, d.metric
        """,
        since_day,
        until_day,
    )
    return [dict(r) for r in rows]
