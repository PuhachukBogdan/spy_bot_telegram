"""The sign-in page and the ``/start dashboard`` deep link.

Both must work with nothing configured in BotFather: the Login Widget needs
/setdomain (a step only the bot owner can take), so the page leads with a
``t.me/<bot>?start=dashboard`` button that the bot answers with a one-time
sign-in link. These tests pin that the button is always there, that the widget
is opt-in, and that the payload changes nothing for anyone who may not read the
report (cover).
"""

from __future__ import annotations

from contextlib import asynccontextmanager
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock

import pytest
from aiogram.filters import CommandObject

from src.bot.handlers import dm_commands as dm
from src.config import settings

# ---------------------------------------------------------------------------
# Sign-in page
# ---------------------------------------------------------------------------


def test_login_page_leads_with_the_bot_deep_link(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(settings, "DASHBOARD_TELEGRAM_WIDGET", False)
    from src.main import _login_page

    html = _login_page("affops_helper_bot")
    assert 'href="https://t.me/affops_helper_bot?start=dashboard"' in html
    assert "telegram-widget.js" not in html  # needs /setdomain; off by default
    assert "/dashboard" in html  # the manual route is still spelled out


def test_login_page_widget_is_opt_in(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(settings, "DASHBOARD_TELEGRAM_WIDGET", True)
    from src.main import _login_page

    html = _login_page("affops_helper_bot")
    assert "telegram-widget.js" in html
    assert 'data-telegram-login="affops_helper_bot"' in html
    assert "?start=dashboard" in html  # the deep link stays primary


def test_login_page_without_username_still_explains_the_bot_route(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(settings, "DASHBOARD_TELEGRAM_WIDGET", True)
    from src.main import _login_page

    html = _login_page(None, error="That link has expired.")
    assert "/dashboard" in html
    assert "t.me/" not in html
    assert "telegram-widget.js" not in html  # no username → no widget even if on
    assert "That link has expired." in html


# ---------------------------------------------------------------------------
# /start dashboard
# ---------------------------------------------------------------------------


class _Msg:
    def __init__(self, tg_id: int | None) -> None:
        self.from_user = SimpleNamespace(id=tg_id) if tg_id is not None else None
        self.answers: list[str] = []

    async def answer(self, text: str, **kwargs: Any) -> None:
        self.answers.append(text)


def _start(args: str | None) -> CommandObject:
    return CommandObject(prefix="/", command="start", args=args)


@pytest.fixture
def start_deps(monkeypatch: pytest.MonkeyPatch) -> AsyncMock:
    @asynccontextmanager
    async def fake_acquire() -> Any:
        yield None

    find = AsyncMock(return_value=None)
    monkeypatch.setattr(dm, "acquire_connection", fake_acquire)
    monkeypatch.setattr(dm, "find_internal_user_by_telegram_id", find)
    monkeypatch.setattr(dm, "issue_login_token", lambda user_id: "TOKEN123")
    return find


@pytest.mark.asyncio
async def test_start_dashboard_signs_in_a_head(start_deps: AsyncMock, mk: Any) -> None:
    start_deps.return_value = mk.user(role="head", tg_id=1000000001)
    msg = _Msg(1000000001)

    await dm.cmd_start(msg, command=_start("dashboard"))  # type: ignore[arg-type]

    assert len(msg.answers) == 1
    assert "/auth/link/TOKEN123" in msg.answers[0]
    assert "team's numbers" in msg.answers[0]


@pytest.mark.asyncio
async def test_start_dashboard_signs_in_an_admin(start_deps: AsyncMock, mk: Any) -> None:
    start_deps.return_value = mk.user(role="admin", tg_id=9)
    msg = _Msg(9)

    await dm.cmd_start(msg, command=_start("dashboard"))  # type: ignore[arg-type]

    assert "/auth/link/TOKEN123" in msg.answers[0]
    assert "whole team" in msg.answers[0]


@pytest.mark.asyncio
async def test_start_dashboard_is_a_plain_start_for_a_manager(
    start_deps: AsyncMock, mk: Any
) -> None:
    # Cover: the page's button must not reveal anything to a manager.
    start_deps.return_value = mk.user(role="manager", tg_id=42)
    msg = _Msg(42)

    await dm.cmd_start(msg, command=_start("dashboard"))  # type: ignore[arg-type]

    assert msg.answers == [dm._START_COVER]


@pytest.mark.asyncio
async def test_start_dashboard_is_a_plain_start_for_an_outsider(start_deps: AsyncMock) -> None:
    msg = _Msg(7)

    await dm.cmd_start(msg, command=_start("dashboard"))  # type: ignore[arg-type]

    assert msg.answers == [dm._START_COVER]


@pytest.mark.asyncio
async def test_bare_start_is_unchanged(start_deps: AsyncMock, mk: Any) -> None:
    start_deps.return_value = mk.user(role="admin", tg_id=9)
    msg = _Msg(9)

    await dm.cmd_start(msg, command=_start(None))  # type: ignore[arg-type]

    assert msg.answers == [dm._START_ADMIN]
