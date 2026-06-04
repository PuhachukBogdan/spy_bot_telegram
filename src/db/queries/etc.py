"""misc queries. Phase 2+.

Currently holds ``internal_users`` lookups (identity resolution for DM commands
and authorization checks). Each takes an already-acquired ``asyncpg.Connection``.
"""

from __future__ import annotations

from uuid import UUID

import asyncpg

from src.db.models import InternalUser


async def get_internal_user_by_id(
    conn: asyncpg.Connection, user_id: UUID
) -> InternalUser | None:
    """Return an internal user by primary key (incl. disabled), for owner display."""
    row = await conn.fetchrow("SELECT * FROM internal_users WHERE id = $1", user_id)
    return InternalUser.from_record(row) if row is not None else None


async def find_internal_user_by_identifier(
    conn: asyncpg.Connection, identifier: str
) -> InternalUser | None:
    """Resolve an enabled internal user from a free-form identifier (``/set_owner``).

    An all-digits identifier is treated as a Telegram user id (matched against the
    ``telegram_accounts`` JSONB array); anything else is matched against
    ``full_name`` case-insensitively (exact, not substring — this drives an
    ownership mutation, so a loose match would be dangerous). Telegram *usernames*
    are not stored on ``internal_users``, so they are not resolvable here.
    """
    identifier = identifier.strip()
    if identifier.isdigit():
        row = await conn.fetchrow(
            """
            SELECT * FROM internal_users
            WHERE telegram_accounts @> $1::jsonb AND enabled = true
            LIMIT 1
            """,
            [int(identifier)],
        )
    else:
        row = await conn.fetchrow(
            """
            SELECT * FROM internal_users
            WHERE full_name ILIKE $1 AND enabled = true
            ORDER BY full_name
            LIMIT 1
            """,
            identifier,
        )
    return InternalUser.from_record(row) if row is not None else None


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


async def list_admin_users(conn: asyncpg.Connection) -> list[InternalUser]:
    """Return all enabled admins (for onboarding DM notifications, CLAUDE.md 7.2).

    Matches on ``role = 'admin'`` — the single source of truth since migration
    0007 (the legacy ``is_admin`` column is no longer read in app code). A person
    whose ``telegram_accounts`` is empty still matches, but the bot can only DM
    accounts that have started it; the caller handles unreachable users.
    """
    rows = await conn.fetch(
        """
        SELECT *
        FROM internal_users
        WHERE role = 'admin'
          AND enabled = true
        ORDER BY full_name ASC
        """
    )
    return [InternalUser.from_record(row) for row in rows]
