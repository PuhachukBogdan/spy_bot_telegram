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


async def get_internal_user_by_telegram_id_any(
    conn: asyncpg.Connection, telegram_user_id: int
) -> InternalUser | None:
    """Resolve a Telegram user id to an internal user INCLUDING disabled ones.

    Unlike :func:`find_internal_user_by_telegram_id` (enabled-only, used as the
    access gate), this is for *administration*: before whitelisting we must see a
    disabled row so ``/add_manager`` can re-enable it instead of failing on the
    unique-ish overlap. Same JSONB containment lookup, no ``enabled`` filter.
    """
    row = await conn.fetchrow(
        "SELECT * FROM internal_users WHERE telegram_accounts @> $1::jsonb LIMIT 1",
        [telegram_user_id],
    )
    return InternalUser.from_record(row) if row is not None else None


async def create_internal_user(
    conn: asyncpg.Connection,
    *,
    full_name: str,
    telegram_id: int,
    role: str = "manager",
) -> InternalUser:
    """Whitelist a new internal user with a single Telegram account (``/add_manager``).

    The list param rides the pool's jsonb codec → stored as a one-element
    ``telegram_accounts`` array. ``role`` is constrained by the DB CHECK
    (admin/manager/viewer). Caller wraps this in a transaction with the audit row.
    """
    row = await conn.fetchrow(
        """
        INSERT INTO internal_users (full_name, role, telegram_accounts, enabled)
        VALUES ($1, $2, $3, true)
        RETURNING *
        """,
        full_name,
        role,
        [telegram_id],
    )
    return InternalUser.from_record(row)


async def set_user_enabled(
    conn: asyncpg.Connection, internal_id: UUID, enabled: bool
) -> InternalUser | None:
    """Flip ``internal_users.enabled`` (disable revokes trust; enable restores it).

    A disabled user is treated as an outsider by the access gate, so disabling
    immediately stops their commands AND makes any chat they later add go through
    the pending/approve path instead of auto-activating. Returns the row, or
    ``None`` if the id was unknown.
    """
    row = await conn.fetchrow(
        "UPDATE internal_users SET enabled = $2 WHERE id = $1 RETURNING *",
        internal_id,
        enabled,
    )
    return InternalUser.from_record(row) if row is not None else None


async def list_internal_users(
    conn: asyncpg.Connection, *, include_disabled: bool = False
) -> list[InternalUser]:
    """List internal users for ``/users`` (enabled-only by default), ordered by role."""
    rows = await conn.fetch(
        """
        SELECT * FROM internal_users
        WHERE ($1::bool OR enabled = true)
        ORDER BY role ASC, full_name ASC
        """,
        include_disabled,
    )
    return [InternalUser.from_record(row) for row in rows]


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
