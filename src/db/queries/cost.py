"""cost_tracking queries. Phase 7+.

Daily spend accounting. The Whisper worker records transcription cost here in
Phase 7; the LLM client (Phase 8) and the circuit breaker (Phase 14) reuse the
same table. Takes an already-acquired ``asyncpg.Connection``.

Money is ``Decimal`` end to end (CLAUDE.md section 9: never float for cost);
asyncpg maps it to the NUMERIC columns directly.
"""

from __future__ import annotations

from decimal import Decimal
from typing import cast

import asyncpg

from src.db.models import CostTracking


async def get_today(conn: asyncpg.Connection) -> CostTracking | None:
    """Today's cost row (``/cost_status``), or ``None`` if nothing spent yet."""
    row = await conn.fetchrow("SELECT * FROM cost_tracking WHERE date = CURRENT_DATE")
    return CostTracking.from_record(row) if row is not None else None


async def sum_last_7_days(conn: asyncpg.Connection) -> Decimal:
    """Total spend over the last 7 calendar days (today inclusive)."""
    value = await conn.fetchval(
        """
        SELECT coalesce(sum(total_cost_usd), 0)
        FROM cost_tracking
        WHERE date >= CURRENT_DATE - 6
        """
    )
    return cast("Decimal", value)


async def record_llm_cost(
    conn: asyncpg.Connection, cost_usd: Decimal, calls: int = 1
) -> None:
    """Add LLM spend (OpenRouter Tier-2/priority) to today's row, creating it if
    absent.

    Mirrors :func:`record_whisper_cost`: upsert on the ``date`` PK so concurrent
    workers accumulate; ``total_cost_usd`` is GENERATED, never written here.
    """
    await conn.execute(
        """
        INSERT INTO cost_tracking (date, llm_cost_usd, llm_calls_count)
        VALUES (CURRENT_DATE, $1, $2)
        ON CONFLICT (date) DO UPDATE
        SET llm_cost_usd = cost_tracking.llm_cost_usd + EXCLUDED.llm_cost_usd,
            llm_calls_count = cost_tracking.llm_calls_count
                              + EXCLUDED.llm_calls_count
        """,
        cost_usd,
        calls,
    )


async def trip_circuit_breaker(conn: asyncpg.Connection) -> bool:
    """Set circuit_breaker_triggered=true for today's row.

    Returns True if the row was newly tripped (was false before), False if it
    was already open or there is no row for today. Uses a targeted UPDATE so
    concurrent workers can't double-trip.
    """
    result: str = await conn.execute(
        """
        UPDATE cost_tracking
        SET circuit_breaker_triggered = true
        WHERE date = CURRENT_DATE AND NOT circuit_breaker_triggered
        """
    )
    return result == "UPDATE 1"


async def is_circuit_open(conn: asyncpg.Connection) -> bool:
    """True if today's circuit breaker is tripped."""
    val = await conn.fetchval(
        "SELECT circuit_breaker_triggered FROM cost_tracking WHERE date = CURRENT_DATE"
    )
    return bool(val)


async def record_whisper_cost(
    conn: asyncpg.Connection, cost_usd: Decimal, calls: int = 1
) -> None:
    """Add Whisper spend to today's row, creating it if absent.

    Upsert on the ``date`` primary key so concurrent workers accumulate rather
    than overwrite. ``total_cost_usd`` is a GENERATED column, so it is never
    written here — Postgres keeps it in sync.
    """
    await conn.execute(
        """
        INSERT INTO cost_tracking (date, whisper_cost_usd, whisper_calls_count)
        VALUES (CURRENT_DATE, $1, $2)
        ON CONFLICT (date) DO UPDATE
        SET whisper_cost_usd = cost_tracking.whisper_cost_usd
                               + EXCLUDED.whisper_cost_usd,
            whisper_calls_count = cost_tracking.whisper_calls_count
                                  + EXCLUDED.whisper_calls_count
        """,
        cost_usd,
        calls,
    )
