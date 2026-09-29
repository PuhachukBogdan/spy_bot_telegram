"""/link_account — admin-only, plan first, apply on confirm (spec 001)."""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any
from unittest.mock import AsyncMock, MagicMock
from uuid import uuid4

import pytest

from src.bot.handlers import dm_commands as dm
from src.db.models import InternalUser
from src.db.queries.account_links import LinkPlan


def _user(name: str = "Mirror | Old", role: str = "manager") -> InternalUser:
    return InternalUser(
        id=uuid4(),
        full_name=name,
        role=role,  # type: ignore[arg-type]
        telegram_accounts=[6000000001],
        created_at=datetime(2026, 7, 1, tzinfo=UTC),
    )


ADMIN = _user("Admin", "admin")
TARGET = _user()


def _message() -> MagicMock:
    msg = MagicMock()
    msg.from_user = MagicMock(id=6000000099)
    msg.answer = AsyncMock()
    return msg


def _command(args: str | None) -> MagicMock:
    cmd = MagicMock()
    cmd.args = args
    return cmd


class _Txn:
    def __init__(self, events: list[str]) -> None:
        self.events = events

    async def __aenter__(self) -> None:
        self.events.append("begin")

    async def __aexit__(self, *exc: Any) -> bool:
        self.events.append("end")
        return False


@pytest.fixture
def wired(monkeypatch: pytest.MonkeyPatch) -> dict[str, Any]:
    state: dict[str, Any] = {"events": [], "plan_status": "link", "applied": 0}
    conn = MagicMock()
    conn.transaction = lambda: _Txn(state["events"])

    class _Acquire:
        async def __aenter__(self) -> Any:
            return conn

        async def __aexit__(self, *exc: Any) -> None:
            return None

    async def fake_find(c: Any, identifier: str) -> InternalUser | None:
        return TARGET if identifier in ("6000000001", "Mirror | Old") else None

    async def fake_plan(c: Any, primary: InternalUser, tg: int) -> LinkPlan:
        state["events"].append("plan")
        dup = _user("Mirror | New") if state["plan_status"] == "link" else None
        return LinkPlan(
            tg,
            primary,
            dup,
            state["plan_status"],
            reason="the target person is disabled" if state["plan_status"] == "refused" else None,
            references={"chats.authorized_by": 72},
            relabel_messages=3,
        )

    async def fake_apply(c: Any, plan: LinkPlan, *, via: str) -> dict[str, int]:
        state["events"].append(f"apply:{via}")
        state["applied"] += 1
        return {"chats.authorized_by": 72, "relabelled_messages": 3}

    monkeypatch.setattr(dm, "acquire_connection", lambda: _Acquire())
    monkeypatch.setattr(dm, "find_internal_user_by_identifier", fake_find)
    monkeypatch.setattr(dm, "plan_account_link", fake_plan)
    monkeypatch.setattr(dm, "apply_account_link", fake_apply)
    return state


@pytest.fixture(autouse=True)
def _bypass_rbac(monkeypatch: pytest.MonkeyPatch) -> None:
    """Let the real wrapper run with an admin actor."""
    from src.bot.middleware import roles

    class _Acquire:
        async def __aenter__(self) -> Any:
            return MagicMock()

        async def __aexit__(self, *exc: Any) -> None:
            return None

    async def fake_actor(conn: Any, uid: int) -> InternalUser:
        return ADMIN

    monkeypatch.setattr(roles, "acquire_connection", lambda: _Acquire())
    monkeypatch.setattr(roles, "get_actor_internal_user", fake_actor)


async def _run(args: str | None) -> MagicMock:
    msg = _message()
    await dm.cmd_link_account(msg, command=_command(args))
    return msg


@pytest.mark.parametrize("args", [None, "", "6000000002", "abc 6000000001", "confirm"])
async def test_usage_on_bad_arguments(wired: dict[str, Any], args: str | None) -> None:
    msg = await _run(args)
    text = msg.answer.call_args[0][0]
    assert "Usage:" in text and "1000000002" in text
    assert wired["events"] == []


async def test_without_confirm_it_only_shows_the_plan(wired: dict[str, Any]) -> None:
    msg = await _run("6000000002 6000000001")
    assert wired["applied"] == 0
    text = msg.answer.call_args[0][0]
    assert "Mirror | New" in text and "72 chats" in text and "confirm" in text


async def test_confirm_applies_once_inside_the_transaction(wired: dict[str, Any]) -> None:
    msg = await _run("6000000002 Mirror | Old confirm")
    assert wired["events"] == ["begin", "plan", "apply:command", "end"]
    assert msg.answer.await_count == 1  # replied after the transaction closed
    assert "✅ Linked" in msg.answer.call_args[0][0]


async def test_noop_and_refused_texts(wired: dict[str, Any]) -> None:
    wired["plan_status"] = "noop"
    msg = await _run("6000000002 6000000001 confirm")
    assert msg.answer.call_args[0][0] == "Already linked — nothing to do."
    wired["plan_status"] = "refused"
    msg = await _run("6000000002 6000000001 confirm")
    assert "Not linked: the target person is disabled" in msg.answer.call_args[0][0]
    assert wired["applied"] == 0


async def test_unknown_target(wired: dict[str, Any]) -> None:
    msg = await _run("6000000002 Nobody Here")
    assert "No such user" in msg.answer.call_args[0][0]


async def test_a_manager_gets_the_unknown_command_line(
    wired: dict[str, Any], monkeypatch: pytest.MonkeyPatch
) -> None:
    from src.bot.middleware import roles

    async def manager_actor(conn: Any, uid: int) -> InternalUser:
        return _user("Some Manager", "manager")

    monkeypatch.setattr(roles, "get_actor_internal_user", manager_actor)
    monkeypatch.setattr(roles, "insert_audit_log", AsyncMock())
    msg = await _run("6000000002 6000000001 confirm")
    assert msg.answer.call_args[0][0] == "Command not found."
    assert wired["events"] == []


async def test_help_all_lists_the_command() -> None:
    assert "/link_account" in dm._HELP_ADMIN
