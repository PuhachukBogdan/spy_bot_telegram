"""chats queries. Phase 2+ (topic-aware since the forum-topics change).

Plain-SQL helpers over the ``chats`` table. A row is a monitored *unit* =
``(telegram_chat_id, topic)`` where ``topic`` is a forum topic id or ``None``
for the whole group (see ``supabase/migrations/0005_topic_units.sql`` and the
wiki plan ``proj1-tgbot-topic-separation-plan``). Lookups match on the generated
``topic_key = COALESCE(message_thread_id, 0)`` so a ``None`` thread is NULL-safe.

Each helper takes an already-acquired ``asyncpg.Connection`` so the caller
controls the pool/transaction boundary.
"""

from __future__ import annotations

from datetime import datetime
from typing import cast
from uuid import UUID

import asyncpg

from src.db.models import Chat, ChatAdderSummary, ChatOverview


async def list_chats_overview(
    conn: asyncpg.Connection,
    *,
    status: str | None = None,
    owner_id: UUID | None = None,
) -> list[ChatOverview]:
    """List units with partner name + last activity for ``/chats``.

    ``status`` filters by unit status (``None`` = all). ``owner_id`` restricts to
    units of one manager's partners (admins pass ``None``). ``last_activity`` is
    the latest message timestamp for the unit.
    """
    rows = await conn.fetch(
        """
        SELECT c.id, c.telegram_chat_id, c.unit_type, c.message_thread_id,
               c.chat_name, c.status, p.name AS partner_name,
               max(m.timestamp) AS last_activity
        FROM chats c
        LEFT JOIN partners p ON p.id = c.partner_id
        LEFT JOIN messages m ON m.chat_id = c.id
        WHERE ($1::text IS NULL OR c.status = $1)
          AND ($2::uuid IS NULL
               OR c.partner_id IN (
                   SELECT id FROM partners WHERE owner_manager_id = $2))
        GROUP BY c.id, p.name
        ORDER BY max(m.timestamp) DESC NULLS LAST
        """,
        status,
        owner_id,
    )
    return [ChatOverview.from_record(row) for row in rows]


async def list_chat_overviews_by_partner(
    conn: asyncpg.Connection, partner_id: UUID
) -> list[ChatOverview]:
    """Units of one partner (all statuses) with last activity, for ``/partner``."""
    rows = await conn.fetch(
        """
        SELECT c.id, c.telegram_chat_id, c.unit_type, c.message_thread_id,
               c.chat_name, c.status, p.name AS partner_name,
               max(m.timestamp) AS last_activity
        FROM chats c
        LEFT JOIN partners p ON p.id = c.partner_id
        LEFT JOIN messages m ON m.chat_id = c.id
        WHERE c.partner_id = $1
        GROUP BY c.id, p.name
        ORDER BY max(m.timestamp) DESC NULLS LAST
        """,
        partner_id,
    )
    return [ChatOverview.from_record(row) for row in rows]


async def find_chats_by_ref(
    conn: asyncpg.Connection, ref: str, ref_as_int: int | None
) -> list[Chat]:
    """Resolve a ``/chat`` reference to matching units (may be more than one).

    A reference can be the full ``chats.id`` UUID, its first 8 chars, the
    ``telegram_chat_id`` (passed pre-parsed as ``ref_as_int``; a supergroup id
    matches every topic unit), or the ``chat_name`` (case-insensitive). The caller
    decides what to do when several units match.
    """
    rows = await conn.fetch(
        """
        SELECT * FROM chats
        WHERE id::text = $1
           OR left(id::text, 8) = $1
           OR ($2::bigint IS NOT NULL AND telegram_chat_id = $2)
           OR chat_name ILIKE $1
        ORDER BY topic_key
        """,
        ref,
        ref_as_int,
    )
    return [Chat.from_record(row) for row in rows]


async def get_chat_status(
    conn: asyncpg.Connection, telegram_chat_id: int, thread_id: int | None
) -> str | None:
    """Return ``chats.status`` for a monitored unit, or ``None`` if unknown.

    Used by the whitelist middleware to gate ingestion: only ``'active'`` units
    are processed. Backed by the UNIQUE index on ``(telegram_chat_id, topic_key)``.
    """
    # asyncpg is untyped, so fetchval is Any; the column is TEXT NOT NULL.
    return cast(
        "str | None",
        await conn.fetchval(
            """
            SELECT status FROM chats
            WHERE telegram_chat_id = $1 AND topic_key = COALESCE($2, 0)
            """,
            telegram_chat_id,
            thread_id,
        ),
    )


async def get_chat_unit(
    conn: asyncpg.Connection, telegram_chat_id: int, thread_id: int | None
) -> Chat | None:
    """Return the full ``Chat`` row for a monitored unit, or ``None``."""
    row = await conn.fetchrow(
        """
        SELECT * FROM chats
        WHERE telegram_chat_id = $1 AND topic_key = COALESCE($2, 0)
        """,
        telegram_chat_id,
        thread_id,
    )
    return Chat.from_record(row) if row is not None else None


async def get_chat_by_id(conn: asyncpg.Connection, chat_id: UUID) -> Chat | None:
    """Return a chat unit by its primary key, or ``None`` (Tier-2 worker)."""
    row = await conn.fetchrow("SELECT * FROM chats WHERE id = $1", chat_id)
    return Chat.from_record(row) if row is not None else None


async def update_chat_last_processed(
    conn: asyncpg.Connection, chat_id: UUID, ts: datetime
) -> None:
    """Advance the Tier-2 analysis watermark (migration 0009); never move it back.

    ``GREATEST`` guards against a re-ordered / duplicate task setting an older
    timestamp, so the watermark only ever moves forward.
    """
    await conn.execute(
        """
        UPDATE chats
        SET last_processed_at = GREATEST(COALESCE(last_processed_at, $2), $2)
        WHERE id = $1
        """,
        chat_id,
        ts,
    )


async def get_by_unit(
    conn: asyncpg.Connection, telegram_chat_id: int, topic_key: int = 0
) -> Chat | None:
    """Return the ``Chat`` for a unit by its stored ``topic_key`` directly.

    Complements :func:`get_chat_unit`: that one takes a possibly-``None`` thread
    id and NULL-coalesces it; this one takes the already-resolved ``topic_key``
    (0 = whole group / General topic).
    """
    row = await conn.fetchrow(
        "SELECT * FROM chats WHERE telegram_chat_id = $1 AND topic_key = $2",
        telegram_chat_id,
        topic_key,
    )
    return Chat.from_record(row) if row is not None else None


async def create_pending_chat(
    conn: asyncpg.Connection,
    telegram_chat_id: int,
    thread_id: int | None,
    chat_name: str | None,
    added_by_user_id: int | None,
    topic_name: str | None = None,
    unit_type: str = "group",
) -> Chat | None:
    """Insert a freshly-discovered unit as ``status='pending'`` (onboarding step).

    ``unit_type`` is ``'group'`` for a group-level unit (bot added to a group) or
    ``'topic'`` for a forum topic discovered from its first message.

    Idempotent against Telegram's webhook retries, re-adds, and repeated messages
    in the same topic via ``ON CONFLICT (telegram_chat_id, topic_key) DO NOTHING``:
    returns the new ``Chat`` row on first insert, or ``None`` if the unit already
    exists. The caller uses the ``None`` result to skip re-notifying admins.
    """
    row = await conn.fetchrow(
        """
        INSERT INTO chats (
            telegram_chat_id, message_thread_id, topic_name,
            chat_name, added_by_user_id, status, unit_type
        )
        VALUES ($1, $2, $3, $4, $5, 'pending', $6)
        ON CONFLICT (telegram_chat_id, topic_key) DO NOTHING
        RETURNING *
        """,
        telegram_chat_id,
        thread_id,
        topic_name,
        chat_name,
        added_by_user_id,
        unit_type,
    )
    return Chat.from_record(row) if row is not None else None


async def create_active_chat(
    conn: asyncpg.Connection,
    *,
    telegram_chat_id: int,
    thread_id: int | None,
    chat_name: str | None,
    added_by_user_id: int | None,
    authorized_by: UUID,
    unit_type: str = "group",
) -> Chat | None:
    """Insert a unit straight as ``status='active'`` — trusted-adder onboarding.

    Used when a *known internal user* adds the bot to a group: we trust the user
    (they passed verification once) rather than approve every chat, so the unit is
    live immediately with ``authorized_by`` = the adder and ``authorized_at=now()``.
    No partner is bound yet — an admin attaches one later with ``/bind_partner``.
    Idempotent via ``ON CONFLICT DO NOTHING`` (returns ``None`` on a re-add of a
    unit we already track, so the onboarding handler skips re-notifying).
    """
    row = await conn.fetchrow(
        """
        INSERT INTO chats (
            telegram_chat_id, message_thread_id, chat_name,
            added_by_user_id, status, unit_type, authorized_by, authorized_at
        )
        VALUES ($1, $2, $3, $4, 'active', $5, $6, now())
        ON CONFLICT (telegram_chat_id, topic_key) DO NOTHING
        RETURNING *
        """,
        telegram_chat_id,
        thread_id,
        chat_name,
        added_by_user_id,
        unit_type,
        authorized_by,
    )
    return Chat.from_record(row) if row is not None else None


async def create_business_chat(
    conn: asyncpg.Connection,
    *,
    telegram_chat_id: int,
    business_connection_id: str,
    business_peer_user_id: int,
    partner_id: UUID | None,
    status: str,
    chat_name: str | None = None,
    authorized_by: UUID | None = None,
) -> Chat | None:
    """Insert a Telegram-Business monitored unit (migration 0006).

    For a business unit ``telegram_chat_id`` holds the partner's TG user_id (a
    private chat has ``chat.id == user.id``), so ``topic_key`` is 0 and the
    existing ``UNIQUE(telegram_chat_id, topic_key)`` keeps each partner DM
    distinct. ``status`` is ``'active'`` for an auto-linked known contact or
    ``'pending'`` for an unknown one; ``authorized_at`` is stamped only when
    active. Idempotent via ``ON CONFLICT DO NOTHING`` — returns ``None`` if a unit
    for this id already exists (lost a race with a concurrent first message).
    """
    row = await conn.fetchrow(
        """
        INSERT INTO chats (
            telegram_chat_id, message_thread_id, chat_name, status, unit_type,
            business_connection_id, business_peer_user_id, partner_id,
            authorized_by, authorized_at
        )
        VALUES ($1, NULL, $2, $3, 'business', $4, $5, $6, $7,
                CASE WHEN $3 = 'active' THEN now() ELSE NULL END)
        ON CONFLICT (telegram_chat_id, topic_key) DO NOTHING
        RETURNING *
        """,
        telegram_chat_id,
        chat_name,
        status,
        business_connection_id,
        business_peer_user_id,
        partner_id,
        authorized_by,
    )
    return Chat.from_record(row) if row is not None else None


async def link_business_chat(
    conn: asyncpg.Connection,
    *,
    telegram_chat_id: int,
    business_connection_id: str,
    business_peer_user_id: int,
    partner_id: UUID,
    authorized_by: UUID,
    chat_name: str | None = None,
) -> Chat | None:
    """Activate (or create) a business unit and bind it to a partner (``/link_business_chat``).

    Upserts on ``(telegram_chat_id, topic_key)``: flips a ``'pending'`` unit (left
    by the first message from an unknown contact) to ``'active'`` and attaches the
    partner, or creates the unit active if none exists yet. The ``DO UPDATE`` is
    guarded to ``unit_type='business'`` so it can never hijack a group/topic unit
    (business peer ids are positive, group ids negative — they don't collide, but
    the guard is cheap insurance and makes a hijack return ``None``).
    """
    row = await conn.fetchrow(
        """
        INSERT INTO chats (
            telegram_chat_id, message_thread_id, chat_name, status, unit_type,
            business_connection_id, business_peer_user_id, partner_id,
            authorized_by, authorized_at
        )
        VALUES ($1, NULL, $2, 'active', 'business', $3, $4, $5, $6, now())
        ON CONFLICT (telegram_chat_id, topic_key) DO UPDATE
        SET status = 'active',
            partner_id = $5,
            business_connection_id = $3,
            business_peer_user_id = $4,
            authorized_by = $6,
            authorized_at = now(),
            chat_name = COALESCE(EXCLUDED.chat_name, chats.chat_name)
        WHERE chats.unit_type = 'business'
        RETURNING *
        """,
        telegram_chat_id,
        chat_name,
        business_connection_id,
        business_peer_user_id,
        partner_id,
        authorized_by,
    )
    return Chat.from_record(row) if row is not None else None


async def list_pending(conn: asyncpg.Connection) -> list[Chat]:
    """Return all units awaiting authorization, oldest first (for ``/pending``)."""
    rows = await conn.fetch(
        "SELECT * FROM chats WHERE status = 'pending' ORDER BY created_at ASC"
    )
    return [Chat.from_record(row) for row in rows]


async def list_pending_chats(conn: asyncpg.Connection) -> list[Chat]:
    """Backwards-compatible alias for :func:`list_pending` (used by ``/pending``)."""
    return await list_pending(conn)


async def list_pending_topics(conn: asyncpg.Connection) -> list[Chat]:
    """Pending units that are forum topics specifically (``unit_type = 'topic'``).

    ``unit_type`` is stamped by the onboarding/ingestion layer (migration 0006);
    until that layer writes it, freshly-discovered units default to ``'group'``,
    so this returns rows only once topic typing is actually being set.
    """
    rows = await conn.fetch(
        """
        SELECT * FROM chats
        WHERE status = 'pending' AND unit_type = 'topic'
        ORDER BY created_at ASC
        """
    )
    return [Chat.from_record(row) for row in rows]


async def authorize_chat(
    conn: asyncpg.Connection,
    telegram_chat_id: int,
    thread_id: int | None,
    partner_id: UUID,
    authorized_by: UUID,
) -> Chat | None:
    """Activate a pending unit and bind it to a partner (``/authorize``).

    Only flips a unit that is still ``'pending'`` (the WHERE guard makes a double
    ``/authorize`` a no-op that returns ``None``), stamping ``authorized_by`` /
    ``authorized_at`` for the audit trail.
    """
    row = await conn.fetchrow(
        """
        UPDATE chats
        SET status = 'active',
            partner_id = $3,
            authorized_by = $4,
            authorized_at = now()
        WHERE telegram_chat_id = $1
          AND topic_key = COALESCE($2, 0)
          AND status = 'pending'
        RETURNING *
        """,
        telegram_chat_id,
        thread_id,
        partner_id,
        authorized_by,
    )
    return Chat.from_record(row) if row is not None else None


async def bind_partner_to_chat(
    conn: asyncpg.Connection,
    *,
    telegram_chat_id: int,
    thread_id: int | None,
    partner_id: UUID,
) -> Chat | None:
    """Attach (or re-attach) a partner to an already-active unit (``/bind_partner``).

    Decoupled from authorization: a trusted-adder unit goes live with no partner,
    and the admin binds one afterwards. Guarded to ``status='active'`` so it never
    silently re-activates a removed/banned unit. Returns the row, or ``None`` if no
    active unit matched.
    """
    row = await conn.fetchrow(
        """
        UPDATE chats
        SET partner_id = $3
        WHERE telegram_chat_id = $1
          AND topic_key = COALESCE($2, 0)
          AND status = 'active'
        RETURNING *
        """,
        telegram_chat_id,
        thread_id,
        partner_id,
    )
    return Chat.from_record(row) if row is not None else None


async def deactivate_chat(
    conn: asyncpg.Connection, telegram_chat_id: int, thread_id: int | None
) -> Chat | None:
    """Disconnect a live unit (``/chat_delete``): set ``status='removed'``.

    Distinct from ``'banned'`` (reject of a *pending* group) and ``'rejected'``
    (reject of a *pending* topic): ``'removed'`` is an admin deliberately
    disconnecting an already-active/pending unit during oversight. Guarded so a
    unit that is already removed/banned/rejected is not re-flipped. The caller
    decides whether to also leave the Telegram chat. Returns the row, or ``None``.
    """
    row = await conn.fetchrow(
        """
        UPDATE chats
        SET status = 'removed'
        WHERE telegram_chat_id = $1
          AND topic_key = COALESCE($2, 0)
          AND status NOT IN ('removed', 'banned', 'rejected')
        RETURNING *
        """,
        telegram_chat_id,
        thread_id,
    )
    return Chat.from_record(row) if row is not None else None


async def reject_chat(
    conn: asyncpg.Connection, telegram_chat_id: int, thread_id: int | None
) -> Chat | None:
    """Ban a pending unit (``/reject``); the caller decides whether to leave.

    Guarded to ``'pending'`` so an already-active or already-banned unit is not
    silently re-banned. Returns the updated row, or ``None`` if nothing matched.
    """
    row = await conn.fetchrow(
        """
        UPDATE chats
        SET status = 'banned'
        WHERE telegram_chat_id = $1
          AND topic_key = COALESCE($2, 0)
          AND status = 'pending'
        RETURNING *
        """,
        telegram_chat_id,
        thread_id,
    )
    return Chat.from_record(row) if row is not None else None


async def reject_topic(
    conn: asyncpg.Connection, telegram_chat_id: int, thread_id: int | None
) -> Chat | None:
    """Reject a pending forum-topic unit (``/reject_topic``).

    Sets ``status='rejected'`` (distinct from a group-level ``'banned'``): a
    rejected topic does NOT make the bot leave the supergroup — it stays for the
    other topics. Guarded to a still-``'pending'`` row whose ``unit_type='topic'``
    so it can never flip a group-level unit. Returns the row, or ``None``.
    """
    row = await conn.fetchrow(
        """
        UPDATE chats
        SET status = 'rejected'
        WHERE telegram_chat_id = $1
          AND topic_key = COALESCE($2, 0)
          AND status = 'pending'
          AND unit_type = 'topic'
        RETURNING *
        """,
        telegram_chat_id,
        thread_id,
    )
    return Chat.from_record(row) if row is not None else None


async def count_live_units(conn: asyncpg.Connection, telegram_chat_id: int) -> int:
    """Count not-yet-dismissed units of a supergroup (status pending or active).

    Used to decide whether leaving the whole Telegram supergroup is safe:
    rejecting/abandoning one topic must NOT make the bot leave a supergroup that
    still monitors other topics (leaving would kill them all).
    """
    return cast(
        "int",
        await conn.fetchval(
            """
            SELECT count(*) FROM chats
            WHERE telegram_chat_id = $1 AND status IN ('pending', 'active')
            """,
            telegram_chat_id,
        ),
    )


async def list_stale_pending_chats(
    conn: asyncpg.Connection, older_than: datetime
) -> list[Chat]:
    """Return pending units created before ``older_than`` (abandoned-chat sweep)."""
    rows = await conn.fetch(
        """
        SELECT *
        FROM chats
        WHERE status = 'pending'
          AND created_at < $1
        ORDER BY created_at ASC
        """,
        older_than,
    )
    return [Chat.from_record(row) for row in rows]


async def mark_chat_abandoned(
    conn: asyncpg.Connection, telegram_chat_id: int, thread_id: int | None
) -> Chat | None:
    """Flag a stale pending unit as ``'abandoned'`` (abandoned-chat sweep)."""
    row = await conn.fetchrow(
        """
        UPDATE chats
        SET status = 'abandoned'
        WHERE telegram_chat_id = $1
          AND topic_key = COALESCE($2, 0)
          AND status = 'pending'
        RETURNING *
        """,
        telegram_chat_id,
        thread_id,
    )
    return Chat.from_record(row) if row is not None else None


async def update_chat_telegram_id(
    conn: asyncpg.Connection, old_telegram_chat_id: int, new_telegram_chat_id: int
) -> int:
    """Repoint every unit of a supergroup to its new id after a migration.

    Telegram assigns a fresh chat id on group->supergroup migration; without this
    the old rows orphan and the new id looks unknown (CLAUDE.md 11.6). All topic
    units of the supergroup move together (``topic_key`` is unchanged, so the new
    ``(telegram_chat_id, topic_key)`` pairs stay unique). Returns the row count.
    """
    result = await conn.execute(
        "UPDATE chats SET telegram_chat_id = $2 WHERE telegram_chat_id = $1",
        old_telegram_chat_id,
        new_telegram_chat_id,
    )
    # asyncpg execute returns a tag like "UPDATE 3"; take the trailing count.
    return int(result.split()[-1]) if result else 0


# --- Admin panel (oversight: who connected which chats) ----------------------
# The panel groups live units by the internal user who added the bot. A unit's
# adder is a Telegram user id (chats.added_by_user_id); an internal user owns
# several Telegram accounts (internal_users.telegram_accounts JSONB array), so
# the match is JSONB containment: telegram_accounts @> to_jsonb(added_by_user_id).
# Only live units (active / pending) are surfaced — removed/banned units drop off.


async def list_managers_with_chat_counts(
    conn: asyncpg.Connection,
) -> list[ChatAdderSummary]:
    """Internal users who have added ≥1 live unit, with their unit count (panel home)."""
    rows = await conn.fetch(
        """
        SELECT u.id AS internal_user_id, u.full_name, u.role,
               count(c.id) AS chat_count
        FROM internal_users u
        JOIN chats c
          ON u.telegram_accounts @> to_jsonb(c.added_by_user_id)
         AND c.status IN ('active', 'pending')
        WHERE u.enabled = true
        GROUP BY u.id, u.full_name, u.role
        ORDER BY u.full_name ASC
        """
    )
    return [ChatAdderSummary.from_record(row) for row in rows]


async def count_unattributed_chats(conn: asyncpg.Connection) -> int:
    """Count live units whose adder is unknown / not an internal user (panel home)."""
    return cast(
        "int",
        await conn.fetchval(
            """
            SELECT count(*) FROM chats c
            WHERE c.status IN ('active', 'pending')
              AND NOT EXISTS (
                  SELECT 1 FROM internal_users u
                  WHERE u.telegram_accounts @> to_jsonb(c.added_by_user_id))
            """
        ),
    )


async def list_chats_by_adder(
    conn: asyncpg.Connection, internal_user_id: UUID
) -> list[ChatOverview]:
    """Live units added by one internal user, with partner + last activity (drill-down)."""
    rows = await conn.fetch(
        """
        SELECT c.id, c.telegram_chat_id, c.unit_type, c.message_thread_id,
               c.chat_name, c.status, p.name AS partner_name,
               max(m.timestamp) AS last_activity
        FROM chats c
        JOIN internal_users u
          ON u.id = $1
         AND u.telegram_accounts @> to_jsonb(c.added_by_user_id)
        LEFT JOIN partners p ON p.id = c.partner_id
        LEFT JOIN messages m ON m.chat_id = c.id
        WHERE c.status IN ('active', 'pending')
        GROUP BY c.id, p.name
        ORDER BY c.created_at DESC
        """,
        internal_user_id,
    )
    return [ChatOverview.from_record(row) for row in rows]


async def list_unattributed_chats(
    conn: asyncpg.Connection,
) -> list[ChatOverview]:
    """Live units whose adder is unknown / not an internal user (drill-down)."""
    rows = await conn.fetch(
        """
        SELECT c.id, c.telegram_chat_id, c.unit_type, c.message_thread_id,
               c.chat_name, c.status, p.name AS partner_name,
               max(m.timestamp) AS last_activity
        FROM chats c
        LEFT JOIN partners p ON p.id = c.partner_id
        LEFT JOIN messages m ON m.chat_id = c.id
        WHERE c.status IN ('active', 'pending')
          AND NOT EXISTS (
              SELECT 1 FROM internal_users u
              WHERE u.telegram_accounts @> to_jsonb(c.added_by_user_id))
        GROUP BY c.id, p.name
        ORDER BY c.created_at DESC
        """
    )
    return [ChatOverview.from_record(row) for row in rows]
