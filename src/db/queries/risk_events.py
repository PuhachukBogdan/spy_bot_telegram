"""risk_events queries. Phase 2+ (read/review surface for DM commands).

Listing + lookup + status-review helpers over ``risk_events``. Each takes an
already-acquired ``asyncpg.Connection`` (project-wide convention). Owner scoping
(a manager sees only their partners' risks) is pushed into SQL via an optional
``owner_id`` filter so the caller never over-fetches.
"""

from __future__ import annotations

from typing import Any
from uuid import UUID

import asyncpg

from src.db.models import RiskEvent, RiskEventOverview


async def save_risk_event(
    conn: asyncpg.Connection,
    *,
    risk_type: str,
    risk_level: str,
    base_score: int,
    final_score: int,
    message_id: UUID | None = None,
    partner_id: UUID | None = None,
    chat_id: UUID | None = None,
    sender_id: int | None = None,
    triggered_patterns: dict[str, Any] | list[Any] | None = None,
    context_modifiers: dict[str, Any] | None = None,
    llm_confidence: float | None = None,
    llm_multiplier: float | None = None,
    llm_verdict: str | None = None,
    llm_explanation: str | None = None,
    disagreement: bool = False,
    detected_phrase: str | None = None,
    context_message_ids: list[UUID] | None = None,
    status: str = "new",
) -> RiskEvent:
    """Insert one risk event and return the stored row.

    Low-level write shared by every producer: the batch processor (§7.4) and
    priority lane (§7.5) pass the full LLM-path fields (computed by
    :func:`src.pipeline.scoring.score_finding`); the operational_sla job passes
    only the rule-path fields (``base_score`` == ``final_score``, the ``llm_*``
    arguments left ``None``). JSONB / UUID[] params ride the pool's codecs.
    The caller owns the surrounding transaction (so the insert commits together
    with its cost/audit rows).
    """
    row = await conn.fetchrow(
        """
        INSERT INTO risk_events (
            message_id, partner_id, chat_id, sender_id,
            risk_type, risk_level, triggered_patterns, context_modifiers,
            base_score, llm_confidence, llm_multiplier, llm_verdict,
            llm_explanation, final_score, disagreement, detected_phrase,
            context_message_ids, status
        )
        VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9, $10, $11, $12, $13, $14,
                $15, $16, $17, $18)
        RETURNING *
        """,
        message_id,
        partner_id,
        chat_id,
        sender_id,
        risk_type,
        risk_level,
        triggered_patterns,
        context_modifiers,
        base_score,
        llm_confidence,
        llm_multiplier,
        llm_verdict,
        llm_explanation,
        final_score,
        disagreement,
        detected_phrase,
        context_message_ids,
        status,
    )
    return RiskEvent.from_record(row)


async def list_recent(
    conn: asyncpg.Connection,
    *,
    limit: int = 20,
    partner_id: UUID | None = None,
    owner_id: UUID | None = None,
) -> list[RiskEventOverview]:
    """Most-recent risk events (newest first), with partner name.

    ``partner_id`` narrows to one partner; ``owner_id`` restricts to a manager's
    own partners (admins pass ``None``). Both filters are parameterized and AND
    together, so a manager asking for a specific partner still can't see one they
    don't own.
    """
    rows = await conn.fetch(
        """
        SELECT r.id, r.risk_level, r.risk_type, r.detected_phrase, r.status,
               r.created_at, p.name AS partner_name
        FROM risk_events r
        LEFT JOIN partners p ON p.id = r.partner_id
        WHERE ($1::uuid IS NULL OR r.partner_id = $1)
          AND ($2::uuid IS NULL
               OR r.partner_id IN (
                   SELECT id FROM partners WHERE owner_manager_id = $2))
        ORDER BY r.created_at DESC
        LIMIT $3
        """,
        partner_id,
        owner_id,
        limit,
    )
    return [RiskEventOverview.from_record(row) for row in rows]


async def list_by_chat(
    conn: asyncpg.Connection, chat_id: UUID, limit: int = 5
) -> list[RiskEventOverview]:
    """Most-recent risk events for one chat unit (newest first), with partner name."""
    rows = await conn.fetch(
        """
        SELECT r.id, r.risk_level, r.risk_type, r.detected_phrase, r.status,
               r.created_at, p.name AS partner_name
        FROM risk_events r
        LEFT JOIN partners p ON p.id = r.partner_id
        WHERE r.chat_id = $1
        ORDER BY r.created_at DESC
        LIMIT $2
        """,
        chat_id,
        limit,
    )
    return [RiskEventOverview.from_record(row) for row in rows]


async def get_by_ref(conn: asyncpg.Connection, ref: str) -> RiskEvent | None:
    """Look up a risk event by its full UUID or its first 8 chars (``/risk``).

    ``ref`` is compared as text against the canonical (lowercase) UUID form, so
    the caller should lowercase it. Returns the full row for the detail card.
    """
    row = await conn.fetchrow(
        "SELECT * FROM risk_events WHERE id::text = $1 OR left(id::text, 8) = $1 LIMIT 1",
        ref,
    )
    return RiskEvent.from_record(row) if row is not None else None


async def update_status(
    conn: asyncpg.Connection, risk_event_id: UUID, status: str, reviewed_by: UUID
) -> RiskEvent | None:
    """Set a risk event's review status (``/mark_*``); stamp reviewer + time.

    Returns the updated row, or ``None`` if the id was unknown.
    """
    row = await conn.fetchrow(
        """
        UPDATE risk_events
        SET status = $2, reviewed_by = $3, reviewed_at = now()
        WHERE id = $1
        RETURNING *
        """,
        risk_event_id,
        status,
        reviewed_by,
    )
    return RiskEvent.from_record(row) if row is not None else None
