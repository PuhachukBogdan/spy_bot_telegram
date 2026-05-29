"""processing_queue queries. Phase 6+.

Postgres-as-queue enqueue side (CLAUDE.md tech stack: no Redis). Workers consume
with ``SELECT ... FOR UPDATE SKIP LOCKED`` in later phases (7 whisper, 10
priority). Takes an already-acquired ``asyncpg.Connection``.
"""

from __future__ import annotations

from typing import Any

import asyncpg


async def enqueue_task(
    conn: asyncpg.Connection,
    task_type: str,
    payload: dict[str, Any],
) -> int:
    """Insert a pending task and return its id.

    ``task_type`` is ``whisper_transcribe`` / ``priority_llm`` / ``batch_llm``.
    ``payload`` is passed natively for the pool's jsonb codec. ``scheduled_for``
    defaults to ``now()`` so the task is immediately eligible.
    """
    task_id = await conn.fetchval(
        """
        INSERT INTO processing_queue (task_type, payload)
        VALUES ($1, $2)
        RETURNING id
        """,
        task_type,
        payload,
    )
    return int(task_id)
