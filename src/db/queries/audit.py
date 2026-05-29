"""admin_audit_log queries. Phase 4+.

Every privileged action (chat authorize / reject, risk-event review, etc.) writes
one append-only row here. Takes an already-acquired ``asyncpg.Connection`` so the
write can share the caller's transaction with the action it records.
"""

from __future__ import annotations

from typing import Any
from uuid import UUID

import asyncpg


async def insert_audit_log(
    conn: asyncpg.Connection,
    *,
    action: str,
    actor_user_id: int | None = None,
    actor_internal_id: UUID | None = None,
    target_entity: str | None = None,
    target_id: UUID | None = None,
    payload: dict[str, Any] | None = None,
) -> None:
    """Append one row to ``admin_audit_log``.

    ``action`` is a short verb (``authorize_chat`` / ``reject_chat`` / ...). The
    optional ``payload`` dict is passed natively; the pool's jsonb codec
    (``encoder=json.dumps``) serializes it, and a Python ``None`` becomes SQL NULL
    (codecs are not applied to NULL). Keyword-only past ``conn`` so call sites read
    self-documentingly and field order can't be transposed.
    """
    await conn.execute(
        """
        INSERT INTO admin_audit_log (
            actor_user_id, actor_internal_id, action,
            target_entity, target_id, payload
        )
        VALUES ($1, $2, $3, $4, $5, $6)
        """,
        actor_user_id,
        actor_internal_id,
        action,
        target_entity,
        target_id,
        payload,
    )
