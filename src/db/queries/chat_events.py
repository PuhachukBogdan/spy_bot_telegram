"""chat_events queries. Phase 5+.

Records non-content chat lifecycle events (member join/leave, title change,
group->supergroup migration, unknown party joined). Takes an already-acquired
``asyncpg.Connection``.
"""

from __future__ import annotations

from typing import Any
from uuid import UUID

import asyncpg


async def insert_chat_event(
    conn: asyncpg.Connection,
    *,
    chat_id: UUID,
    event_type: str,
    actor_user_id: int | None = None,
    target_user_id: int | None = None,
    payload: dict[str, Any] | None = None,
) -> None:
    """Append one row to ``chat_events``.

    ``event_type`` is one of ``member_join`` / ``member_leave`` / ``title_change``
    / ``migration`` / ``unknown_party_joined``. ``payload`` is passed natively for
    the pool's jsonb codec; ``None`` becomes SQL NULL.
    """
    await conn.execute(
        """
        INSERT INTO chat_events (chat_id, event_type, actor_user_id, target_user_id, payload)
        VALUES ($1, $2, $3, $4, $5)
        """,
        chat_id,
        event_type,
        actor_user_id,
        target_user_id,
        payload,
    )
