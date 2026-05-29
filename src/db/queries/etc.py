"""misc queries. Phase 2+.

Currently holds ``internal_users`` lookups (identity resolution for DM commands
and authorization checks). Each takes an already-acquired ``asyncpg.Connection``.
"""

from __future__ import annotations

import asyncpg

from src.db.models import InternalUser


async def find_internal_user_by_telegram_id(
    conn: asyncpg.Connection, telegram_user_id: int
) -> InternalUser | None:
    """Resolve a Telegram user id to an enabled internal user, or ``None``.

    ``internal_users.telegram_accounts`` is a JSONB array of Telegram user ids
    (a person may have several accounts). The ``@>`` containment operator is
    served by the GIN index ``idx_internal_users_tg``. Only ``enabled`` users
    match, so a disabled staff member is treated as an outsider.
    """
    row = await conn.fetchrow(
        """
        SELECT *
        FROM internal_users
        WHERE telegram_accounts @> $1::jsonb
          AND enabled = true
        LIMIT 1
        """,
        [telegram_user_id],  # JSON codec on the pool encodes this to '[<id>]'
    )
    return InternalUser.from_record(row) if row is not None else None
