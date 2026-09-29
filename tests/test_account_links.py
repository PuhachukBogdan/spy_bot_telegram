"""Attaching a Telegram account to a person (spec 001-merge-manager-accounts).

No real DB: a scripted fake connection answers by SQL substring and records every
statement, so the tests pin both the decision (plan) and the writes (apply).
Ids are placeholders — real ones never enter the repo.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any
from uuid import UUID, uuid4

import pytest

from src.db.models import InternalUser
from src.db.queries import account_links as al
from src.db.queries.account_links import (
    LinkPlan,
    apply_account_link,
    person_snapshot,
    plan_account_link,
)

OLD_TG = 6000000001
NEW_TG = 6000000002


def _user(
    *,
    name: str = "Mirror | Old",
    accounts: list[int] | None = None,
    role: str = "manager",
    slack: str | None = None,
    enabled: bool = True,
    is_test: bool = False,
    uid: UUID | None = None,
) -> InternalUser:
    return InternalUser(
        id=uid or uuid4(),
        full_name=name,
        role=role,  # type: ignore[arg-type]
        telegram_accounts=[OLD_TG] if accounts is None else accounts,
        slack_user_id=slack,
        enabled=enabled,
        is_test=is_test,
        created_at=datetime(2026, 7, 1, tzinfo=UTC),
    )


def _row(user: InternalUser) -> dict[str, Any]:
    return user.model_dump()


class FakeConn:
    """Answers queries by the first matching SQL fragment; records all calls."""

    def __init__(self) -> None:
        self.holders: list[InternalUser] = []
        self.fk_columns: list[tuple[str, str]] = sorted(al._KNOWN_FK)
        self.ref_counts: dict[str, int] = {}
        self.relabel = 0
        self.calls: list[tuple[str, tuple[Any, ...]]] = []

    async def fetch(self, sql: str, *args: Any) -> list[dict[str, Any]]:
        self.calls.append((sql, args))
        if "FROM internal_users WHERE telegram_accounts @>" in sql:
            return [_row(u) for u in self.holders]
        if "pg_constraint" in sql:
            return [{"tbl": t, "col": c} for t, c in self.fk_columns]
        raise AssertionError(f"unexpected fetch: {sql}")

    async def fetchval(self, sql: str, *args: Any) -> Any:
        self.calls.append((sql, args))
        if "FROM messages" in sql and "sender_role = 'partner'" in sql and "count(*)" in sql:
            return self.relabel
        for key, value in self.ref_counts.items():
            table, column = key.split(".")
            if f"FROM {table} WHERE {column} = $1" in sql:
                return value
        if "SELECT count(*) FROM" in sql and "WHERE" in sql and "= $1" in sql:
            return 0
        if "jsonb_agg" in sql:
            return [OLD_TG, NEW_TG]
        return 0

    async def execute(self, sql: str, *args: Any) -> str:
        self.calls.append((sql, args))
        if sql.lstrip().startswith("UPDATE messages"):
            return f"UPDATE {self.relabel}"
        if "DELETE FROM manager_tone_daily" in sql:
            return f"DELETE {self.ref_counts.get('manager_tone_daily.manager_id', 0)}"
        for key, value in self.ref_counts.items():
            table, column = key.split(".")
            if sql.startswith(f"UPDATE {table} SET {column}"):
                return f"UPDATE {value}"
        return "UPDATE 1"

    def executed(self, fragment: str) -> list[tuple[str, tuple[Any, ...]]]:
        return [(s, a) for s, a in self.calls if fragment in s]


@pytest.fixture
def conn() -> FakeConn:
    return FakeConn()


# --- plan -----------------------------------------------------------------


async def test_account_already_on_the_person_is_a_noop(conn: FakeConn) -> None:
    primary = _user(accounts=[OLD_TG, NEW_TG])
    plan = await plan_account_link(conn, primary, NEW_TG)  # type: ignore[arg-type]
    assert plan.status == "noop"
    assert conn.calls == []  # decided without touching the DB


async def test_account_with_no_record_links_without_a_duplicate(conn: FakeConn) -> None:
    conn.relabel = 261
    plan = await plan_account_link(conn, _user(), NEW_TG)  # type: ignore[arg-type]
    assert plan.status == "link"
    assert plan.duplicate is None
    assert plan.slack_action == "none"
    assert plan.references == {}
    assert plan.relabel_messages == 261


@pytest.mark.parametrize(
    ("primary_slack", "dup_slack", "expected"),
    [
        (None, "U0TEST00002", "move"),
        ("U0TEST00001", "U0TEST00001", "keep_primary"),
        ("U0TEST00001", "U0TEST00002", "conflict"),
        ("U0TEST00001", None, "none"),
    ],
)
async def test_slack_rule(
    conn: FakeConn, primary_slack: str | None, dup_slack: str | None, expected: str
) -> None:
    dup = _user(name="Mirror | New", accounts=[NEW_TG], slack=dup_slack)
    conn.holders = [dup]
    conn.ref_counts = {"chats.authorized_by": 72, "manager_tone_daily.manager_id": 5}
    plan = await plan_account_link(conn, _user(slack=primary_slack), NEW_TG)  # type: ignore[arg-type]
    assert plan.status == "link"
    assert plan.duplicate is not None and plan.duplicate.id == dup.id
    assert plan.slack_action == expected
    assert plan.references == {"chats.authorized_by": 72, "manager_tone_daily.manager_id": 5}


@pytest.mark.parametrize(
    ("primary_kwargs", "dup_kwargs", "holders", "fragment"),
    [
        ({"enabled": False}, None, 0, "disabled"),
        ({"is_test": True}, None, 0, "test account"),
        ({}, {"accounts": [NEW_TG, 6000000009]}, 1, "holds 2 accounts"),
        ({}, {"role": "head"}, 1, "never changes roles"),
        ({}, {"role": "admin"}, 1, "never changes roles"),
        ({}, {}, 2, "held by 2 records"),
    ],
)
async def test_refusals(
    conn: FakeConn,
    primary_kwargs: dict[str, Any],
    dup_kwargs: dict[str, Any] | None,
    holders: int,
    fragment: str,
) -> None:
    dup_kwargs = {"accounts": [NEW_TG], **(dup_kwargs or {})}
    conn.holders = [_user(name=f"dup{i}", **dup_kwargs) for i in range(holders)]
    plan = await plan_account_link(conn, _user(**primary_kwargs), NEW_TG)  # type: ignore[arg-type]
    assert plan.status == "refused"
    assert plan.reason is not None and fragment in plan.reason


async def test_an_unknown_foreign_key_refuses_instead_of_orphaning(conn: FakeConn) -> None:
    conn.holders = [_user(name="dup", accounts=[NEW_TG])]
    conn.fk_columns = [*conn.fk_columns, ("brand_new_table", "owner_id")]
    plan = await plan_account_link(conn, _user(), NEW_TG)  # type: ignore[arg-type]
    assert plan.status == "refused"
    assert plan.reason is not None and "brand_new_table.owner_id" in plan.reason


async def test_the_holder_being_the_primary_itself_is_a_noop(conn: FakeConn) -> None:
    primary = _user()
    conn.holders = [primary]
    # Stale model (account missing locally) but the DB says it is already theirs.
    plan = await plan_account_link(conn, primary, NEW_TG)  # type: ignore[arg-type]
    assert plan.status == "noop"


# --- apply ----------------------------------------------------------------


def _merge_plan(conn: FakeConn, *, slack: str = "move") -> LinkPlan:
    dup = _user(name="Mirror | New", accounts=[NEW_TG], slack="U0TEST00002")
    conn.ref_counts = {
        "chats.authorized_by": 72,
        "admin_audit_log.actor_internal_id": 73,
        "manager_tone_daily.manager_id": 5,
    }
    conn.relabel = 3
    return LinkPlan(
        NEW_TG,
        _user(),
        dup,
        "link",
        slack_action=slack,  # type: ignore[arg-type]
        references=dict(conn.ref_counts),
        relabel_messages=3,
    )


async def test_apply_frees_the_slack_id_before_giving_it_to_the_person(conn: FakeConn) -> None:
    plan = _merge_plan(conn)
    await apply_account_link(conn, plan, via="script")  # type: ignore[arg-type]
    slack_writes = [i for i, (s, _) in enumerate(conn.calls) if "SET slack_user_id" in s]
    cleared, assigned = slack_writes[0], slack_writes[1]
    assert conn.calls[cleared][1] == (plan.duplicate.id,)  # type: ignore[union-attr]
    assert conn.calls[assigned][1] == (plan.primary.id, "U0TEST00002")
    assert cleared < assigned


async def test_apply_moves_every_reference_and_merges_tone_by_sum(conn: FakeConn) -> None:
    plan = _merge_plan(conn)
    moved = await apply_account_link(conn, plan, via="script")  # type: ignore[arg-type]
    assert moved["chats.authorized_by"] == 72
    assert moved["admin_audit_log.actor_internal_id"] == 73
    assert moved["manager_tone_daily.manager_id"] == 5
    merge = conn.executed("INSERT INTO manager_tone_daily")
    assert merge and "flagged = manager_tone_daily.flagged + EXCLUDED.flagged" in merge[0][0]
    assert "assessed = manager_tone_daily.assessed + EXCLUDED.assessed" in merge[0][0]
    # tone-daily is never UPDATEd in place: that would collide on its primary key
    assert not conn.executed("UPDATE manager_tone_daily SET manager_id")


async def test_apply_retires_the_duplicate_without_deleting_it(conn: FakeConn) -> None:
    plan = _merge_plan(conn)
    await apply_account_link(conn, plan, via="script")  # type: ignore[arg-type]
    retire = conn.executed("SET enabled = false")
    assert retire and retire[0][1] == (plan.duplicate.id,)  # type: ignore[union-attr]
    assert "telegram_accounts = '[]'::jsonb" in retire[0][0]
    assert not conn.executed("DELETE FROM internal_users")


async def test_apply_appends_the_account_only_if_missing(conn: FakeConn) -> None:
    plan = _merge_plan(conn)
    await apply_account_link(conn, plan, via="script")  # type: ignore[arg-type]
    (append_sql, append_args), = conn.executed("|| to_jsonb($2::bigint)")
    assert "NOT (COALESCE(telegram_accounts, '[]'::jsonb) @> to_jsonb($2::bigint))" in append_sql
    assert append_args == (plan.primary.id, NEW_TG)


async def test_relabel_touches_only_live_partner_rows(conn: FakeConn) -> None:
    plan = _merge_plan(conn)
    moved = await apply_account_link(conn, plan, via="script")  # type: ignore[arg-type]
    (sql, args), = conn.executed("UPDATE messages SET sender_role = 'internal'")
    assert "sender_role = 'partner'" in sql and "source <> 'imported'" in sql
    assert args == (NEW_TG,)
    assert moved["relabelled_messages"] == 3


async def test_apply_audits_on_the_person_not_on_the_retired_row(conn: FakeConn) -> None:
    plan = _merge_plan(conn)
    await apply_account_link(conn, plan, via="command")  # type: ignore[arg-type]
    (sql, args), = conn.executed("INSERT INTO admin_audit_log")
    assert args[1] == plan.primary.id  # actor_internal_id
    assert args[2] == "account_linked"
    payload = args[5]
    assert payload["from_user"] == str(plan.duplicate.id)  # type: ignore[union-attr]
    assert payload["via"] == "command"


async def test_apply_never_writes_a_role(conn: FakeConn) -> None:
    plan = _merge_plan(conn)
    await apply_account_link(conn, plan, via="script")  # type: ignore[arg-type]
    user_writes = [s for s, _ in conn.calls if "UPDATE internal_users" in s]
    assert user_writes
    assert not [s for s in user_writes if " role =" in s or " role=" in s]


async def test_link_without_a_duplicate_only_appends_and_relabels(conn: FakeConn) -> None:
    conn.relabel = 261
    plan = LinkPlan(NEW_TG, _user(), None, "link", relabel_messages=261)
    moved = await apply_account_link(conn, plan, via="script")  # type: ignore[arg-type]
    assert moved == {"relabelled_messages": 261}
    assert not conn.executed("SET enabled = false")
    assert not conn.executed("SET slack_user_id")


@pytest.mark.parametrize("status", ["noop", "refused"])
async def test_only_a_link_plan_can_be_applied(conn: FakeConn, status: str) -> None:
    plan = LinkPlan(NEW_TG, _user(), None, status)  # type: ignore[arg-type]
    with pytest.raises(ValueError):
        await apply_account_link(conn, plan, via="script")  # type: ignore[arg-type]
    assert conn.calls == []


async def test_snapshot_counts_every_account_of_the_person(conn: FakeConn) -> None:
    snap = await person_snapshot(conn, [uuid4()])  # type: ignore[arg-type]
    assert snap.accounts == [OLD_TG, NEW_TG]
    message_calls = [a for s, a in conn.calls if "FROM messages" in s and "sender_role = $3" in s]
    assert message_calls and all(a[1] == [OLD_TG, NEW_TG] for a in message_calls)
