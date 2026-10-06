"""Who of OUR people is in which partner chat (``chat_members``, migration 0027).

The table is a cache of Telegram's own answer plus the evidence the bot sees in
passing. Three writers, in order of authority:

* :func:`record_verified_status` — the membership sweep asked ``getChatMember``
  and got Telegram's status. Overwrites whatever was there.
* :func:`record_event_status` — a join / leave the bot witnessed (service
  message or ``chat_member`` update). Sets the status it saw.
* :func:`touch_presence` — a staff message in the chat. Proof of presence at
  that moment: refreshes ``last_seen_at`` and, if the row said ``left``, flips
  it back to ``member`` (they are evidently in). Never downgrades.

Rows are never deleted on leaving: ``first_seen_at`` is what tells an old
account from a new one on the dashboard, and that must survive the person
leaving a chat or the sweep finding them gone.

Only internal accounts are written here. Partners are not tracked.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Any
from uuid import UUID

import asyncpg

#: Telegram statuses that mean "in the chat right now".
PRESENT_STATUSES: frozenset[str] = frozenset(
    {"creator", "administrator", "member", "restricted"}
)


def is_present(status: str | None) -> bool:
    return status in PRESENT_STATUSES


@dataclass(frozen=True)
class StaffAccount:
    """One Telegram account of one real person, as the sweep walks them."""

    telegram_user_id: int
    internal_user_id: UUID
    full_name: str
    role: str
    enabled: bool
    deactivated_at: datetime | None


async def list_staff_accounts(conn: asyncpg.Connection) -> list[StaffAccount]:
    """Every Telegram account of every real person — the sweep's subject list.

    Real = admin / head / manager with a non-empty ``telegram_accounts`` and not
    a test row. Disabled and deactivated people are INCLUDED on purpose: whether
    a departed colleague is still sitting in partner chats is exactly the kind
    of fact the sweep exists to surface.
    """
    rows = await conn.fetch(
        """
        SELECT u.id, u.full_name, u.role, u.enabled, u.deactivated_at,
               (acc.value)::bigint AS telegram_user_id
        FROM internal_users u
        CROSS JOIN LATERAL jsonb_array_elements_text(
            COALESCE(u.telegram_accounts, '[]'::jsonb)) AS acc(value)
        WHERE u.role IN ('admin', 'head', 'manager')
          AND COALESCE(u.is_test, false) = false
          AND acc.value ~ '^[0-9]+$'
        ORDER BY u.full_name, telegram_user_id
        """
    )
    return [
        StaffAccount(
            telegram_user_id=r["telegram_user_id"],
            internal_user_id=r["id"],
            full_name=r["full_name"],
            role=r["role"],
            enabled=r["enabled"],
            deactivated_at=r["deactivated_at"],
        )
        for r in rows
    ]


@dataclass(frozen=True)
class SweepTarget:
    """One Telegram group to ask about, with every unit row that shares its id."""

    telegram_chat_id: int
    unit_ids: tuple[UUID, ...]
    chat_name: str | None


async def list_sweep_targets(conn: asyncpg.Connection) -> list[SweepTarget]:
    """Active groups and forum topics, grouped by Telegram chat id.

    Membership is a property of the Telegram chat, so a forum's topics share one
    answer; the title too. Business (private) units are not groups and are left
    out. Test chats are swept like any other — presence there is still a fact.
    """
    rows = await conn.fetch(
        """
        SELECT telegram_chat_id,
               array_agg(id ORDER BY topic_key) AS unit_ids,
               min(chat_name) AS chat_name
        FROM chats
        WHERE status = 'active' AND unit_type IN ('group', 'topic')
        GROUP BY telegram_chat_id
        ORDER BY telegram_chat_id
        """
    )
    return [
        SweepTarget(
            telegram_chat_id=r["telegram_chat_id"],
            unit_ids=tuple(r["unit_ids"]),
            chat_name=r["chat_name"],
        )
        for r in rows
    ]


async def record_verified_status(
    conn: asyncpg.Connection,
    *,
    chat_id: UUID,
    telegram_user_id: int,
    internal_user_id: UUID | None,
    status: str,
    verified_at: datetime,
) -> None:
    """Store Telegram's answer for one (chat, account). Authoritative."""
    await conn.execute(
        """
        INSERT INTO chat_members (chat_id, telegram_user_id, internal_user_id, status,
                                  first_seen_at, last_seen_at, last_verified_at,
                                  source, updated_at)
        VALUES ($1, $2, $3, $4, $5, $5, $5, 'sweep', now())
        ON CONFLICT (chat_id, telegram_user_id) DO UPDATE
            SET status = EXCLUDED.status,
                internal_user_id = COALESCE(EXCLUDED.internal_user_id,
                                            chat_members.internal_user_id),
                last_seen_at = CASE WHEN EXCLUDED.status IN
                        ('creator', 'administrator', 'member', 'restricted')
                    THEN GREATEST(chat_members.last_seen_at, EXCLUDED.last_seen_at)
                    ELSE chat_members.last_seen_at END,
                last_verified_at = EXCLUDED.last_verified_at,
                source = 'sweep',
                updated_at = now()
        """,
        chat_id,
        telegram_user_id,
        internal_user_id,
        status,
        verified_at,
    )


async def record_event_status(
    conn: asyncpg.Connection,
    *,
    chat_id: UUID,
    telegram_user_id: int,
    internal_user_id: UUID | None,
    status: str,
    at: datetime,
) -> None:
    """A join or leave the bot witnessed. ``status`` is Telegram's new status."""
    await conn.execute(
        """
        INSERT INTO chat_members (chat_id, telegram_user_id, internal_user_id, status,
                                  first_seen_at, last_seen_at, source, updated_at)
        VALUES ($1, $2, $3, $4, $5, $5, 'event', now())
        ON CONFLICT (chat_id, telegram_user_id) DO UPDATE
            SET status = EXCLUDED.status,
                internal_user_id = COALESCE(EXCLUDED.internal_user_id,
                                            chat_members.internal_user_id),
                first_seen_at = LEAST(chat_members.first_seen_at, EXCLUDED.first_seen_at),
                last_seen_at = CASE WHEN EXCLUDED.status IN
                        ('creator', 'administrator', 'member', 'restricted')
                    THEN GREATEST(chat_members.last_seen_at, EXCLUDED.last_seen_at)
                    ELSE chat_members.last_seen_at END,
                source = 'event',
                updated_at = now()
        """,
        chat_id,
        telegram_user_id,
        internal_user_id,
        status,
        at,
    )


async def touch_presence(
    conn: asyncpg.Connection,
    *,
    chat_id: UUID,
    telegram_user_id: int,
    internal_user_id: UUID | None,
    at: datetime,
) -> None:
    """A staff message landed: the account is in the chat at ``at``.

    Creates the row as ``member`` if unknown, revives a ``left`` row (they are
    evidently back), and never touches a present status — a sweep that found
    them an ``administrator`` keeps saying so.
    """
    await conn.execute(
        """
        INSERT INTO chat_members (chat_id, telegram_user_id, internal_user_id, status,
                                  first_seen_at, last_seen_at, source, updated_at)
        VALUES ($1, $2, $3, 'member', $4, $4, 'message', now())
        ON CONFLICT (chat_id, telegram_user_id) DO UPDATE
            SET status = CASE WHEN chat_members.status IN
                        ('creator', 'administrator', 'member', 'restricted')
                    THEN chat_members.status ELSE 'member' END,
                internal_user_id = COALESCE(EXCLUDED.internal_user_id,
                                            chat_members.internal_user_id),
                first_seen_at = LEAST(chat_members.first_seen_at, EXCLUDED.first_seen_at),
                last_seen_at = GREATEST(chat_members.last_seen_at, EXCLUDED.last_seen_at),
                source = CASE WHEN chat_members.status IN
                        ('creator', 'administrator', 'member', 'restricted')
                    THEN chat_members.source ELSE 'message' END,
                updated_at = now()
        """,
        chat_id,
        telegram_user_id,
        internal_user_id,
        at,
    )


async def list_present_memberships(conn: asyncpg.Connection) -> list[dict[str, Any]]:
    """Current presence of real people in ACTIVE, non-test chats — the metrics' crew map.

    One row per (chat, account): ``chat_id``, ``telegram_user_id``,
    ``internal_user_id``, ``first_seen_at``. Only present statuses. The caller
    maps accounts to roster managers and drops anyone it does not measure.
    """
    rows = await conn.fetch(
        """
        SELECT cm.chat_id, cm.telegram_user_id, cm.internal_user_id, cm.first_seen_at
        FROM chat_members cm
        JOIN chats c ON c.id = cm.chat_id
        WHERE cm.status IN ('creator', 'administrator', 'member', 'restricted')
          AND c.status = 'active'
          AND COALESCE(c.is_test, false) = false
        """
    )
    return [dict(r) for r in rows]


async def first_seen_by_account(conn: asyncpg.Connection) -> dict[int, datetime]:
    """Earliest evidence of each staff account anywhere — the old/new tie-break."""
    rows = await conn.fetch(
        "SELECT telegram_user_id, MIN(first_seen_at) AS first_seen FROM chat_members "
        "GROUP BY telegram_user_id"
    )
    return {r["telegram_user_id"]: r["first_seen"] for r in rows}


async def count_present_by_account(conn: asyncpg.Connection) -> dict[int, int]:
    """``telegram_user_id -> number of active chats they are in`` (``/users``)."""
    rows = await conn.fetch(
        """
        SELECT cm.telegram_user_id, COUNT(*) AS chats
        FROM chat_members cm
        JOIN chats c ON c.id = cm.chat_id
        WHERE cm.status IN ('creator', 'administrator', 'member', 'restricted')
          AND c.status = 'active'
        GROUP BY cm.telegram_user_id
        """
    )
    return {r["telegram_user_id"]: r["chats"] for r in rows}
