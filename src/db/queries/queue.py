"""processing_queue queries. Phase 6+.

Postgres-as-queue (CLAUDE.md tech stack: no Redis). The enqueue side lands in
Phase 6; the consume side (``claim_tasks`` + the completion helpers) lands in
Phase 7 for the Whisper worker and is reused by the priority/batch workers
(phases 9/10). Takes an already-acquired ``asyncpg.Connection``.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any

import asyncpg

from src.db.models import ProcessingQueue


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


async def claim_tasks(
    conn: asyncpg.Connection, task_type: str, limit: int
) -> list[ProcessingQueue]:
    """Atomically claim up to ``limit`` eligible pending tasks of ``task_type``.

    A single ``UPDATE ... WHERE id IN (SELECT ... FOR UPDATE SKIP LOCKED)``
    statement flips the chosen rows to ``in_progress`` (bumping ``attempts`` and
    ``last_attempt_at``) and returns them. ``FOR UPDATE SKIP LOCKED`` lets
    multiple workers claim disjoint batches without blocking each other; because
    the whole claim is one statement, no surrounding transaction is needed and
    the row locks are released immediately, so slow downstream work (download,
    API call) never holds a DB lock.

    Only ``scheduled_for <= now()`` rows are eligible, so a rescheduled (backed
    off) retry stays invisible until its time comes.
    """
    rows = await conn.fetch(
        """
        UPDATE processing_queue
        SET status = 'in_progress',
            attempts = attempts + 1,
            last_attempt_at = now()
        WHERE id IN (
            SELECT id FROM processing_queue
            WHERE task_type = $1
              AND status = 'pending'
              AND scheduled_for <= now()
            ORDER BY scheduled_for
            FOR UPDATE SKIP LOCKED
            LIMIT $2
        )
        RETURNING *
        """,
        task_type,
        limit,
    )
    return [ProcessingQueue.from_record(row) for row in rows]


async def complete_task(conn: asyncpg.Connection, task_id: int) -> None:
    """Mark a claimed task done and clear any prior error."""
    await conn.execute(
        """
        UPDATE processing_queue
        SET status = 'done', completed_at = now(), error = NULL
        WHERE id = $1
        """,
        task_id,
    )


async def fail_task(conn: asyncpg.Connection, task_id: int, error: str) -> None:
    """Give up on a task permanently (attempts exhausted): mark it ``failed``."""
    await conn.execute(
        "UPDATE processing_queue SET status = 'failed', error = $2 WHERE id = $1",
        task_id,
        error,
    )


async def retry_task(
    conn: asyncpg.Connection, task_id: int, error: str, run_at: datetime
) -> None:
    """Return a task to the queue for a later retry (back off via ``run_at``).

    Sets it back to ``pending`` with a future ``scheduled_for`` so ``claim_tasks``
    skips it until the backoff elapses, recording the last error for visibility.
    """
    await conn.execute(
        """
        UPDATE processing_queue
        SET status = 'pending', scheduled_for = $3, error = $2
        WHERE id = $1
        """,
        task_id,
        error,
        run_at,
    )
