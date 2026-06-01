"""cost_tracking queries. Phase 7+.

Daily spend accounting. The Whisper worker records transcription cost here in
Phase 7; the LLM client (Phase 8) and the circuit breaker (Phase 14) reuse the
same table. Takes an already-acquired ``asyncpg.Connection``.

Money is ``Decimal`` end to end (CLAUDE.md section 9: never float for cost);
asyncpg maps it to the NUMERIC columns directly.
"""

from __future__ import annotations

from decimal import Decimal

import asyncpg


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
