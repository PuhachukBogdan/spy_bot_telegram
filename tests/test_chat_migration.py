"""Group -> supergroup migration: repointing units without losing monitoring.

Regression cover for a prod failure that was silent on every surface. All three
migrations the bot had ever seen ended with the partner unmonitored:

    my_chat_member (new supergroup)  -> on_bot_added creates a unit at the NEW id,
                                        attributed to whoever triggered the
                                        migration (the partner) -> status 'pending'
    migrate_to_chat_id (old chat)    -> repoint raises on
                                        UNIQUE (telegram_chat_id, topic_key)

leaving the authoritative unit stranded on a dead id while still reading
'active', and the duplicate swept to 'abandoned' 7 days later with the bot
leaving the chat.

The fake table below enforces that unique constraint on purpose: a fake that
did not could not reproduce the bug, and these tests would prove nothing.
"""

from __future__ import annotations

from typing import Any
from uuid import UUID, uuid4

import pytest

from src.db.queries.chats import migrate_chat_telegram_id

OLD_ID = -5103573059
NEW_ID = -1003856224182


class _UniqueViolation(Exception):
    """Stands in for asyncpg UniqueViolationError on chats_chat_topic_key."""


class _ChatsTable:
    """In-memory ``chats`` supporting exactly the statements the repoint issues."""

    def __init__(self, rows: list[dict[str, Any]]) -> None:
        self.rows = [dict(r) for r in rows]
        self._enforce()

    # --- helpers ----------------------------------------------------------
    def _enforce(self) -> None:
        seen: set[tuple[int, int]] = set()
        for row in self.rows:
            key = (row["telegram_chat_id"], row["topic_key"])
            if key in seen:
                raise _UniqueViolation(f"duplicate (telegram_chat_id, topic_key)={key}")
            seen.add(key)

    def by_id(self, row_id: UUID) -> dict[str, Any]:
        return next(r for r in self.rows if r["id"] == row_id)

    def at(
        self, telegram_chat_id: int, *, exclude_merged: bool = False
    ) -> list[dict[str, Any]]:
        return [
            r
            for r in self.rows
            if r["telegram_chat_id"] == telegram_chat_id
            and not (exclude_merged and r["status"] == "merged")
        ]

    # --- asyncpg surface --------------------------------------------------
    async def fetch(self, sql: str, *args: Any) -> list[dict[str, Any]]:
        q = " ".join(sql.split())
        merged_filtered = "status <> 'merged'" in q
        if q.startswith("SELECT topic_key FROM chats"):
            rows = self.at(args[0], exclude_merged=merged_filtered)
            return [{"topic_key": r["topic_key"]} for r in rows]
        if q.startswith("SELECT id, topic_key FROM chats"):
            rows = self.at(args[0], exclude_merged=merged_filtered)
            return [{"id": r["id"], "topic_key": r["topic_key"]} for r in rows]
        raise AssertionError(f"unexpected fetch: {q}")

    async def fetchval(self, sql: str, *args: Any) -> Any:
        q = " ".join(sql.split())
        if q == "SELECT min(telegram_chat_id) FROM chats":
            return min(r["telegram_chat_id"] for r in self.rows)
        raise AssertionError(f"unexpected fetchval: {q}")

    async def execute(self, sql: str, *args: Any) -> str:
        q = " ".join(sql.split())
        if "status = 'merged' WHERE id = $1" in q:
            row = self.by_id(args[0])
            row["telegram_chat_id"] = args[1]
            row["status"] = "merged"
            touched = 1
        elif "WHERE telegram_chat_id = $1" in q:
            targets = self.at(args[0], exclude_merged="status <> 'merged'" in q)
            for row in targets:
                row["telegram_chat_id"] = args[1]
            touched = len(targets)
        elif "WHERE id = $1" in q:
            self.by_id(args[0])["telegram_chat_id"] = args[1]
            touched = 1
        else:
            raise AssertionError(f"unexpected execute: {q}")
        self._enforce()
        return f"UPDATE {touched}"


def _unit(
    telegram_chat_id: int,
    *,
    topic_key: int = 0,
    status: str = "active",
) -> dict[str, Any]:
    return {
        "id": uuid4(),
        "telegram_chat_id": telegram_chat_id,
        "topic_key": topic_key,
        "status": status,
    }


# --- the prod scenario --------------------------------------------------------


async def test_repoint_parks_the_duplicate_created_by_the_onboarding_race() -> None:
    """The exact prod shape: a pending squatter already holds the new id."""
    survivor = _unit(OLD_ID, status="active")
    squatter = _unit(NEW_ID, status="pending")
    table = _ChatsTable([survivor, squatter])

    outcome = await migrate_chat_telegram_id(table, OLD_ID, NEW_ID)  # type: ignore[arg-type]

    assert outcome.units_moved == 1
    assert outcome.units_parked == 1
    # The authoritative unit now answers on the id Telegram actually routes to,
    # and keeps its status (hence its authorization and partner binding).
    assert table.by_id(survivor["id"])["telegram_chat_id"] == NEW_ID
    assert table.by_id(survivor["id"])["status"] == "active"
    # The duplicate is pushed onto the vacated old id and hidden as 'merged'
    # rather than deleted — ten tables hold FKs on chats(id).
    assert table.by_id(squatter["id"])["telegram_chat_id"] == OLD_ID
    assert table.by_id(squatter["id"])["status"] == "merged"


async def test_repoint_survives_an_abandoned_squatter() -> None:
    """After the 7-day sweep the squatter is 'abandoned', not 'pending'."""
    survivor = _unit(OLD_ID)
    squatter = _unit(NEW_ID, status="abandoned")
    table = _ChatsTable([survivor, squatter])

    outcome = await migrate_chat_telegram_id(table, OLD_ID, NEW_ID)  # type: ignore[arg-type]

    assert (outcome.units_moved, outcome.units_parked) == (1, 1)
    assert table.by_id(survivor["id"])["telegram_chat_id"] == NEW_ID
    assert table.by_id(squatter["id"])["status"] == "merged"


async def test_repoint_without_a_duplicate_is_a_plain_move() -> None:
    survivor = _unit(OLD_ID)
    table = _ChatsTable([survivor])

    outcome = await migrate_chat_telegram_id(table, OLD_ID, NEW_ID)  # type: ignore[arg-type]

    assert (outcome.units_moved, outcome.units_parked) == (1, 0)
    assert table.by_id(survivor["id"])["telegram_chat_id"] == NEW_ID
    assert table.by_id(survivor["id"])["status"] == "active"


# --- idempotency + not clobbering legitimate rows ----------------------------


async def test_repoint_is_idempotent_and_cannot_park_a_legitimate_unit() -> None:
    """A redelivered service message must not park the unit it just moved.

    Telegram retries webhooks, so this path runs twice in practice. Without the
    "nothing at the old id -> do nothing" guard, the second run would treat the
    freshly-migrated unit as a squatter and park it — turning a retry into the
    very outage this function exists to prevent.
    """
    survivor = _unit(OLD_ID)
    squatter = _unit(NEW_ID, status="pending")
    table = _ChatsTable([survivor, squatter])

    await migrate_chat_telegram_id(table, OLD_ID, NEW_ID)  # type: ignore[arg-type]
    again = await migrate_chat_telegram_id(table, OLD_ID, NEW_ID)  # type: ignore[arg-type]

    assert (again.units_moved, again.units_parked) == (0, 0)
    assert table.by_id(survivor["id"])["telegram_chat_id"] == NEW_ID
    assert table.by_id(survivor["id"])["status"] == "active"


async def test_repoint_is_a_noop_when_the_old_id_is_untracked() -> None:
    stranger = _unit(NEW_ID, status="active")
    table = _ChatsTable([stranger])

    outcome = await migrate_chat_telegram_id(table, OLD_ID, NEW_ID)  # type: ignore[arg-type]

    assert (outcome.units_moved, outcome.units_parked) == (0, 0)
    assert table.by_id(stranger["id"])["telegram_chat_id"] == NEW_ID
    assert table.by_id(stranger["id"])["status"] == "active"


# --- forum topics ------------------------------------------------------------


async def test_repoint_moves_every_topic_unit_and_parks_only_collisions() -> None:
    """All topic units move together; only the colliding topic_key is parked."""
    group = _unit(OLD_ID, topic_key=0)
    topic_a = _unit(OLD_ID, topic_key=5)
    topic_b = _unit(OLD_ID, topic_key=9)
    squatter = _unit(NEW_ID, topic_key=0, status="pending")
    table = _ChatsTable([group, topic_a, topic_b, squatter])

    outcome = await migrate_chat_telegram_id(table, OLD_ID, NEW_ID)  # type: ignore[arg-type]

    assert outcome.units_moved == 3
    assert outcome.units_parked == 1
    for unit in (group, topic_a, topic_b):
        assert table.by_id(unit["id"])["telegram_chat_id"] == NEW_ID
    assert table.by_id(squatter["id"])["telegram_chat_id"] == OLD_ID


async def test_repoint_leaves_a_genuinely_new_topic_at_the_new_id_alone() -> None:
    """A topic discovered after the migration is not a duplicate of anything."""
    group = _unit(OLD_ID, topic_key=0)
    fresh_topic = _unit(NEW_ID, topic_key=77, status="active")
    table = _ChatsTable([group, fresh_topic])

    outcome = await migrate_chat_telegram_id(table, OLD_ID, NEW_ID)  # type: ignore[arg-type]

    assert (outcome.units_moved, outcome.units_parked) == (1, 0)
    assert table.by_id(group["id"])["telegram_chat_id"] == NEW_ID
    assert table.by_id(fresh_topic["id"])["telegram_chat_id"] == NEW_ID
    assert table.by_id(fresh_topic["id"])["status"] == "active"


async def test_parking_several_topic_duplicates_never_collides() -> None:
    """Two squatters must not be parked onto the same intermediate id."""
    group = _unit(OLD_ID, topic_key=0)
    topic = _unit(OLD_ID, topic_key=5)
    squatter_group = _unit(NEW_ID, topic_key=0, status="pending")
    squatter_topic = _unit(NEW_ID, topic_key=5, status="pending")
    table = _ChatsTable([group, topic, squatter_group, squatter_topic])

    outcome = await migrate_chat_telegram_id(table, OLD_ID, NEW_ID)  # type: ignore[arg-type]

    assert (outcome.units_moved, outcome.units_parked) == (2, 2)
    assert {r["topic_key"] for r in table.at(NEW_ID)} == {0, 5}
    assert all(r["status"] == "active" for r in table.at(NEW_ID))
    assert {r["topic_key"] for r in table.at(OLD_ID)} == {0, 5}
    assert all(r["status"] == "merged" for r in table.at(OLD_ID))


# --- the fake itself must be able to fail ------------------------------------


def test_the_fake_table_enforces_the_unique_constraint() -> None:
    """Guard the guard: without this the tests above would pass vacuously."""
    with pytest.raises(_UniqueViolation):
        _ChatsTable([_unit(NEW_ID, topic_key=0), _unit(NEW_ID, topic_key=0)])
