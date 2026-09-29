"""scripts/link_accounts.py — dry run by default, --apply commits (spec 001)."""

from __future__ import annotations

import argparse
import importlib.util
import sys
from datetime import UTC, datetime
from pathlib import Path
from types import ModuleType
from typing import Any
from uuid import uuid4

import pytest

from src.db.models import InternalUser
from src.db.queries.account_links import LinkPlan, PersonSnapshot


def _load() -> ModuleType:
    path = Path(__file__).resolve().parent.parent / "scripts" / "link_accounts.py"
    spec = importlib.util.spec_from_file_location("link_accounts_script", path)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


script = _load()


def _user() -> InternalUser:
    return InternalUser(
        id=uuid4(),
        full_name="Person",
        telegram_accounts=[6000000001],
        created_at=datetime(2026, 7, 1, tzinfo=UTC),
    )


_SNAP = PersonSnapshot([6000000001], 1, (1, 1), (0, 0), (1, 1), (0, 0), (0, 0), (0, 0))


class _Txn:
    def __init__(self, log: list[str]) -> None:
        self.log = log

    async def __aenter__(self) -> None:
        self.log.append("begin")

    async def __aexit__(self, exc_type: Any, *rest: Any) -> bool:
        self.log.append("rollback" if exc_type else "commit")
        return False


class _Conn:
    def __init__(self, log: list[str]) -> None:
        self.log = log

    def transaction(self) -> _Txn:
        return _Txn(self.log)


def _wire(monkeypatch: pytest.MonkeyPatch, status: str) -> list[str]:
    log: list[str] = []
    user = _user()

    class _Acquire:
        async def __aenter__(self) -> _Conn:
            return _Conn(log)

        async def __aexit__(self, *exc: Any) -> None:
            return None

    async def fake_resolve(conn: Any, ref: str) -> InternalUser:
        return user

    async def fake_plan(conn: Any, primary: InternalUser, tg: int) -> LinkPlan:
        return LinkPlan(tg, primary, None, status, reason="nope" if status == "refused" else None)  # type: ignore[arg-type]

    async def fake_apply(conn: Any, plan: LinkPlan, *, via: str) -> dict[str, int]:
        log.append(f"apply:{via}")
        return {"relabelled_messages": 0}

    async def fake_snapshot(conn: Any, ids: Any) -> PersonSnapshot:
        return _SNAP

    async def fake_close() -> None:
        return None

    monkeypatch.setattr(script, "acquire_connection", lambda: _Acquire())
    monkeypatch.setattr(script, "resolve_primary", fake_resolve)
    monkeypatch.setattr(script, "plan_account_link", fake_plan)
    monkeypatch.setattr(script, "apply_account_link", fake_apply)
    monkeypatch.setattr(script, "person_snapshot", fake_snapshot)
    monkeypatch.setattr(script, "close_pool", fake_close)
    return log


def test_pair_parsing() -> None:
    assert script.parse_pair("6000000001:6000000002") == ("6000000001", 6000000002)
    assert script.parse_pair(" abc-uuid : 7 ") == ("abc-uuid", 7)
    for bad in ("6000000001", ":7", "x:y", "x:"):
        with pytest.raises(argparse.ArgumentTypeError):
            script.parse_pair(bad)


def test_missing_pair_is_an_argument_error() -> None:
    with pytest.raises(SystemExit) as exc:
        script.build_parser().parse_args([])
    assert exc.value.code == 2


async def test_dry_run_applies_then_rolls_back(
    monkeypatch: pytest.MonkeyPatch, capsys: Any
) -> None:
    log = _wire(monkeypatch, "link")
    args = script.build_parser().parse_args(["--pair", "6000000001:6000000002"])
    assert await script._main(args) == 0
    assert log == ["begin", "apply:script", "rollback"]
    assert "DRY RUN (nothing written)" in capsys.readouterr().out


async def test_apply_commits(monkeypatch: pytest.MonkeyPatch, capsys: Any) -> None:
    log = _wire(monkeypatch, "link")
    args = script.build_parser().parse_args(["--pair", "6000000001:6000000002", "--apply"])
    assert await script._main(args) == 0
    assert log == ["begin", "apply:script", "commit"]
    assert "APPLIED" in capsys.readouterr().out


async def test_refused_pair_exits_1_and_applies_nothing(monkeypatch: pytest.MonkeyPatch) -> None:
    log = _wire(monkeypatch, "refused")
    args = script.build_parser().parse_args(["--pair", "6000000001:6000000002", "--apply"])
    assert await script._main(args) == 1
    assert not [e for e in log if e.startswith("apply")]


async def test_noop_exits_0(monkeypatch: pytest.MonkeyPatch, capsys: Any) -> None:
    _wire(monkeypatch, "noop")
    args = script.build_parser().parse_args(["--pair", "6000000001:6000000002"])
    assert await script._main(args) == 0
    assert "0 linked · 1 noop · 0 refused" in capsys.readouterr().out
