"""Attach a Telegram account to an existing person (spec 001-merge-manager-accounts).

One person, one ``internal_users`` row, any number of Telegram accounts — the
``telegram_accounts`` array was built for exactly that, and every reader already
resolves a sender through it (``build_manager_index``, the ``_any`` lookup in
ingest). So attaching an account is: append the id to the primary row.

The awkward case is an account that already has a row of its own — ``/register``
from a new account creates one. Everything stored *by row id* then has to move to
the primary row: every foreign key on ``internal_users(id)`` (read from the
catalog, so a table added later cannot be skipped silently) and the additive tone
counters, which merge by sum because ``(manager_id, day, metric)`` is their key.
The duplicate row is retired, not deleted: disabled, no accounts, no Slack id —
it disappears from every surface and stays for history.

Messages the account wrote while nobody knew it was staff are relabelled
``partner → internal``; the archive (``source='imported'``) keeps its own roles.

Nothing here opens a transaction or commits: callers wrap plan + apply in one
transaction, which is also how a dry run works (apply, snapshot, roll back).
Roles are never touched — a duplicate that holds ``admin``/``head`` is refused.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from typing import Any, Literal
from uuid import UUID

import asyncpg

from src.db.models import InternalUser
from src.db.queries.audit import insert_audit_log

LinkStatus = Literal["link", "noop", "refused"]
SlackAction = Literal["move", "keep_primary", "conflict", "none"]

#: Every foreign key on internal_users(id) this module knows how to move. A
#: catalog FK missing from here refuses the link — better than orphaning rows.
_KNOWN_FK: frozenset[tuple[str, str]] = frozenset(
    {
        ("partners", "owner_manager_id"),
        ("chats", "authorized_by"),
        ("risk_events", "reviewed_by"),
        ("admin_audit_log", "actor_internal_id"),
        ("business_connections", "internal_user_id"),
        ("business_connections", "approved_by"),
        ("notes", "created_by"),
        ("notes", "resolved_by"),
        ("reminders", "target_user_id"),
        ("reminders", "created_by"),
        ("manager_tone_daily", "manager_id"),
        ("manager_tone_flags", "manager_id"),
    }
)
#: Keyed on the person, so a plain UPDATE would collide with the primary's rows.
_MERGE_BY_SUM: frozenset[tuple[str, str]] = frozenset({("manager_tone_daily", "manager_id")})

_WINDOWS_DAYS = (30, 120)


@dataclass(frozen=True)
class LinkPlan:
    telegram_id: int
    primary: InternalUser
    duplicate: InternalUser | None
    status: LinkStatus
    reason: str | None = None
    slack_action: SlackAction = "none"
    #: rows that will move, keyed ``table.column``
    references: dict[str, int] = field(default_factory=dict)
    relabel_messages: int = 0


@dataclass(frozen=True)
class PersonSnapshot:
    accounts: list[int]
    owned_active_chats: int
    #: each metric → (30-day value, 120-day value)
    messages_internal: tuple[int, int]
    messages_partner: tuple[int, int]
    waits_closed: tuple[int, int]
    tone_flagged: tuple[int, int]
    tone_assessed: tuple[int, int]
    risk_events_authored: tuple[int, int]


async def internal_user_fk_columns(conn: asyncpg.Connection) -> list[tuple[str, str]]:
    """(table, column) of every single-column FK pointing at internal_users."""
    rows = await conn.fetch(
        """
        SELECT c.conrelid::regclass::text AS tbl, a.attname AS col
        FROM pg_constraint c
        JOIN pg_attribute a ON a.attrelid = c.conrelid AND a.attnum = ANY (c.conkey)
        WHERE c.contype = 'f' AND c.confrelid = 'internal_users'::regclass
        ORDER BY 1, 2
        """
    )
    return [(str(r["tbl"]), str(r["col"])) for r in rows]


def _slack_action(primary: InternalUser, duplicate: InternalUser | None) -> SlackAction:
    if duplicate is None or not duplicate.slack_user_id:
        return "none"
    if not primary.slack_user_id:
        return "move"
    if primary.slack_user_id == duplicate.slack_user_id:
        return "keep_primary"
    return "conflict"


async def plan_account_link(
    conn: asyncpg.Connection, primary: InternalUser, telegram_id: int
) -> LinkPlan:
    """Work out what linking ``telegram_id`` to ``primary`` would do (reads only)."""

    def refused(reason: str, duplicate: InternalUser | None = None) -> LinkPlan:
        return LinkPlan(telegram_id, primary, duplicate, "refused", reason)

    if telegram_id in primary.telegram_accounts:
        return LinkPlan(telegram_id, primary, None, "noop")
    if not primary.enabled:
        return refused("the target person is disabled")
    if primary.is_test:
        return refused("the target person is a test account")

    holders = [
        InternalUser.from_record(dict(r))
        for r in await conn.fetch(
            "SELECT * FROM internal_users WHERE telegram_accounts @> $1::jsonb",
            [telegram_id],
        )
    ]
    if len(holders) > 1:
        return refused(f"account is held by {len(holders)} records — sort that out first")
    duplicate = holders[0] if holders else None
    if duplicate is not None:
        if duplicate.id == primary.id:
            return LinkPlan(telegram_id, primary, None, "noop")
        if len(duplicate.telegram_accounts) > 1:
            return refused(
                f"{duplicate.full_name} holds {len(duplicate.telegram_accounts)} accounts",
                duplicate,
            )
        if duplicate.role in ("admin", "head"):
            return refused(
                f"{duplicate.full_name} is {duplicate.role} — merging never changes roles",
                duplicate,
            )

    references: dict[str, int] = {}
    if duplicate is not None:
        columns = await internal_user_fk_columns(conn)
        unknown = [f"{t}.{c}" for t, c in columns if (t, c) not in _KNOWN_FK]
        if unknown:
            return refused(f"unknown reference {', '.join(unknown)}", duplicate)
        for table, column in columns:
            count = await conn.fetchval(
                f"SELECT count(*) FROM {table} WHERE {column} = $1", duplicate.id
            )
            if count:
                references[f"{table}.{column}"] = int(count)

    relabel = await conn.fetchval(
        """
        SELECT count(*) FROM messages
        WHERE sender_id = $1 AND sender_role = 'partner' AND source <> 'imported'
        """,
        telegram_id,
    )
    return LinkPlan(
        telegram_id,
        primary,
        duplicate,
        "link",
        slack_action=_slack_action(primary, duplicate),
        references=references,
        relabel_messages=int(relabel or 0),
    )


def _rowcount(status: str) -> int:
    """asyncpg ``execute`` returns ``'UPDATE 3'`` / ``'INSERT 0 5'`` — take the count."""
    try:
        return int(status.rsplit(maxsplit=1)[-1])
    except (ValueError, IndexError):
        return 0


async def apply_account_link(
    conn: asyncpg.Connection, plan: LinkPlan, *, via: str
) -> dict[str, int]:
    """Carry out a ``link`` plan inside the caller's transaction; return row counts."""
    if plan.status != "link":
        raise ValueError(f"cannot apply a {plan.status} plan")
    primary, dup, tg = plan.primary, plan.duplicate, plan.telegram_id
    moved: dict[str, int] = {}

    if dup is not None:
        if plan.slack_action == "move":
            # slack_user_id is UNIQUE: free it on the duplicate before it lands.
            await conn.execute(
                "UPDATE internal_users SET slack_user_id = NULL WHERE id = $1", dup.id
            )
            await conn.execute(
                "UPDATE internal_users SET slack_user_id = $2 WHERE id = $1",
                primary.id,
                dup.slack_user_id,
            )
        for key in plan.references:
            table, column = key.split(".", 1)
            if (table, column) in _MERGE_BY_SUM:
                continue
            moved[key] = _rowcount(
                await conn.execute(
                    f"UPDATE {table} SET {column} = $2 WHERE {column} = $1",
                    dup.id,
                    primary.id,
                )
            )
        if "manager_tone_daily.manager_id" in plan.references:
            await conn.execute(
                """
                INSERT INTO manager_tone_daily (manager_id, day, metric, flagged, assessed)
                SELECT $2, day, metric, flagged, assessed
                FROM manager_tone_daily WHERE manager_id = $1
                ON CONFLICT (manager_id, day, metric) DO UPDATE
                SET flagged = manager_tone_daily.flagged + EXCLUDED.flagged,
                    assessed = manager_tone_daily.assessed + EXCLUDED.assessed,
                    updated_at = now()
                """,
                dup.id,
                primary.id,
            )
            moved["manager_tone_daily.manager_id"] = _rowcount(
                await conn.execute("DELETE FROM manager_tone_daily WHERE manager_id = $1", dup.id)
            )
        # Retire, don't delete: invisible everywhere, still there for history.
        await conn.execute(
            """
            UPDATE internal_users
            SET enabled = false, telegram_accounts = '[]'::jsonb, slack_user_id = NULL
            WHERE id = $1
            """,
            dup.id,
        )

    await conn.execute(
        """
        UPDATE internal_users
        SET telegram_accounts = COALESCE(telegram_accounts, '[]'::jsonb) || to_jsonb($2::bigint)
        WHERE id = $1 AND NOT (COALESCE(telegram_accounts, '[]'::jsonb) @> to_jsonb($2::bigint))
        """,
        primary.id,
        tg,
    )
    relabelled = _rowcount(
        await conn.execute(
            """
            UPDATE messages SET sender_role = 'internal'
            WHERE sender_id = $1 AND sender_role = 'partner' AND source <> 'imported'
            """,
            tg,
        )
    )
    await insert_audit_log(
        conn,
        action="account_linked",
        actor_internal_id=primary.id,
        target_entity="internal_user",
        target_id=primary.id,
        payload={
            "telegram_id": tg,
            "to_user": str(primary.id),
            "from_user": str(dup.id) if dup is not None else None,
            "moved": moved,
            "slack": plan.slack_action,
            "relabelled_messages": relabelled,
            "via": via,
        },
    )
    return {**moved, "relabelled_messages": relabelled}


async def person_snapshot(
    conn: asyncpg.Connection, user_ids: list[UUID], *, now: datetime | None = None
) -> PersonSnapshot:
    """Headline numbers for a person across all their rows and accounts (R6)."""
    now = now or datetime.now(UTC)
    accounts_raw = await conn.fetchval(
        """
        SELECT COALESCE(jsonb_agg(DISTINCT t ORDER BY t), '[]'::jsonb)
        FROM internal_users u, jsonb_array_elements(u.telegram_accounts) t
        WHERE u.id = ANY ($1::uuid[])
        """,
        user_ids,
    )
    accounts = [int(a) for a in (accounts_raw or [])]
    owned = await conn.fetchval(
        "SELECT count(*) FROM chats WHERE authorized_by = ANY ($1::uuid[]) AND status = 'active'",
        user_ids,
    )

    async def per_window(sql: str, *args: Any) -> tuple[int, int]:
        values = []
        for days in _WINDOWS_DAYS:
            values.append(int(await conn.fetchval(sql, now - timedelta(days=days), *args) or 0))
        return values[0], values[1]

    msg_sql = """
        SELECT count(*) FROM messages
        WHERE timestamp >= $1 AND sender_id = ANY ($2::bigint[])
          AND sender_role = $3 AND source <> 'imported'
    """
    waits_sql = """
        SELECT count(*) FROM (
            SELECT m.sender_id, m.sender_role,
                   LAG(m.sender_role) OVER (PARTITION BY m.chat_id ORDER BY m.timestamp) AS prev
            FROM messages m JOIN chats c ON c.id = m.chat_id
            WHERE m.timestamp >= $1 AND m.source <> 'imported'
              AND c.authorized_by = ANY ($3::uuid[])
        ) x
        WHERE x.sender_id = ANY ($2::bigint[]) AND x.sender_role = 'internal'
          AND x.prev IS DISTINCT FROM 'internal' AND x.prev IS NOT NULL
    """
    # `assessed` is repeated on every metric row of a manager-day (it is the
    # shared denominator), so it is summed once per (manager, day).
    flagged_sql = """
        SELECT COALESCE(SUM(flagged), 0) FROM manager_tone_daily
        WHERE day >= $1::date AND manager_id = ANY ($2::uuid[])
    """
    assessed_sql = """
        SELECT COALESCE(SUM(a), 0) FROM (
            SELECT MAX(assessed) AS a FROM manager_tone_daily
            WHERE day >= $1::date AND manager_id = ANY ($2::uuid[])
            GROUP BY manager_id, day
        ) d
    """
    return PersonSnapshot(
        accounts=accounts,
        owned_active_chats=int(owned or 0),
        messages_internal=await per_window(msg_sql, accounts, "internal"),
        messages_partner=await per_window(msg_sql, accounts, "partner"),
        waits_closed=await per_window(waits_sql, accounts, user_ids),
        tone_flagged=await per_window(flagged_sql, user_ids),
        tone_assessed=await per_window(assessed_sql, user_ids),
        risk_events_authored=await per_window(
            "SELECT count(*) FROM risk_events"
            " WHERE created_at >= $1 AND sender_id = ANY ($2::bigint[])",
            accounts,
        ),
    )
