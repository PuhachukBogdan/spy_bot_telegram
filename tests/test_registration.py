"""Tests for the /register Slack OTP flow (registration.py)."""

from __future__ import annotations

import time
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch
from uuid import uuid4

import pytest

from src.alerts.slack import SlackDeliveryError
from src.bot.handlers import registration as reg_mod
from src.bot.handlers.registration import (
    _awaiting_slack_id,
    _pending,
    _PendingReg,
    _step_receive_code,
    _step_receive_slack_id,
)

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _mock_roles(actor: Any) -> Any:
    """Patch the roles middleware so @require_role sees a pre-resolved actor.

    The @require_role wrapper calls acquire_connection() + get_actor_internal_user()
    before invoking the real handler. Patching those two names in the middleware
    module lets cmd_register tests bypass the real DB.
    """
    fake_conn = AsyncMock()

    @asynccontextmanager
    async def _fake_acquire() -> AsyncIterator[Any]:
        yield fake_conn

    return patch.multiple(
        "src.bot.middleware.roles",
        acquire_connection=_fake_acquire,
        get_actor_internal_user=AsyncMock(return_value=actor),
    )


def _fake_message(user_id: int = 1001, text: str = "") -> MagicMock:
    msg = MagicMock()
    msg.from_user = MagicMock()
    msg.from_user.id = user_id
    msg.text = text
    msg.answer = AsyncMock()
    return msg


def _fake_actor(slack_user_id: str | None = None, role: str = "admin") -> MagicMock:
    actor = MagicMock()
    actor.id = uuid4()
    actor.slack_user_id = slack_user_id
    actor.role = role
    return actor


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture(autouse=True)
def clear_state() -> None:  # type: ignore[return]
    """Reset module-level state before each test."""
    _awaiting_slack_id.clear()
    _pending.clear()
    yield
    _awaiting_slack_id.clear()
    _pending.clear()


# ---------------------------------------------------------------------------
# /register command
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_register_sets_awaiting_state() -> None:
    msg = _fake_message(user_id=42)
    actor = _fake_actor()
    with _mock_roles(actor):
        await reg_mod.cmd_register(msg)
    assert 42 in _awaiting_slack_id
    assert _awaiting_slack_id[42] == actor.id
    msg.answer.assert_called_once()
    assert "member ID" in msg.answer.call_args[0][0]


@pytest.mark.asyncio
async def test_register_shows_relink_note_when_already_linked() -> None:
    msg = _fake_message(user_id=42)
    actor = _fake_actor(slack_user_id="U0EXISTING0")
    with _mock_roles(actor):
        await reg_mod.cmd_register(msg)
    assert "replacing" in msg.answer.call_args[0][0]


@pytest.mark.asyncio
async def test_register_clears_existing_pending_state() -> None:
    _pending[42] = _PendingReg(
        internal_user_id=uuid4(), slack_user_id="U0OLDDDDDD", code="AABBCC"
    )
    msg = _fake_message(user_id=42)
    actor = _fake_actor()
    with _mock_roles(actor):
        await reg_mod.cmd_register(msg)
    assert 42 not in _pending
    assert 42 in _awaiting_slack_id


# ---------------------------------------------------------------------------
# /cancel command
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_cancel_clears_awaiting_state() -> None:
    _awaiting_slack_id[42] = uuid4()
    msg = _fake_message(user_id=42)
    await reg_mod.cmd_cancel(msg)
    assert 42 not in _awaiting_slack_id
    msg.answer.assert_called_once()
    assert "cancelled" in msg.answer.call_args[0][0].lower()


@pytest.mark.asyncio
async def test_cancel_clears_pending_state() -> None:
    _pending[42] = _PendingReg(
        internal_user_id=uuid4(), slack_user_id="U0ABCDEFGH", code="AA1122"
    )
    msg = _fake_message(user_id=42)
    await reg_mod.cmd_cancel(msg)
    assert 42 not in _pending
    msg.answer.assert_called_once()


@pytest.mark.asyncio
async def test_cancel_silent_when_no_flow() -> None:
    msg = _fake_message(user_id=99)
    await reg_mod.cmd_cancel(msg)
    msg.answer.assert_not_called()


# ---------------------------------------------------------------------------
# handle_registration_text — routing
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_text_ignored_when_not_in_flow() -> None:
    msg = _fake_message(user_id=55, text="hello")
    await reg_mod.handle_registration_text(msg)
    msg.answer.assert_not_called()


# ---------------------------------------------------------------------------
# _step_receive_slack_id
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_invalid_slack_id_rejected() -> None:
    _awaiting_slack_id[42] = uuid4()
    msg = _fake_message(user_id=42)
    await _step_receive_slack_id(msg, 42, "notanid")
    msg.answer.assert_called_once()
    assert "doesn't look" in msg.answer.call_args[0][0]
    assert 42 in _awaiting_slack_id  # stays in awaiting state


@pytest.mark.asyncio
async def test_slack_id_too_short_rejected() -> None:
    _awaiting_slack_id[42] = uuid4()
    msg = _fake_message(user_id=42)
    await _step_receive_slack_id(msg, 42, "U0123")  # too short
    assert "doesn't look" in msg.answer.call_args[0][0]


@pytest.mark.asyncio
@patch("src.bot.handlers.registration.send_dm_to_user", new_callable=AsyncMock)
async def test_valid_slack_id_sends_otp(mock_send: AsyncMock) -> None:
    uid = uuid4()
    _awaiting_slack_id[42] = uid
    msg = _fake_message(user_id=42)

    await _step_receive_slack_id(msg, 42, "U01234ABCDE")

    mock_send.assert_called_once()
    assert mock_send.call_args[0][0] == "U01234ABCDE"
    assert 42 not in _awaiting_slack_id
    assert 42 in _pending
    assert _pending[42].slack_user_id == "U01234ABCDE"
    assert _pending[42].internal_user_id == uid
    assert len(_pending[42].code) == 6
    msg.answer.assert_called_once()
    assert "6-character" in msg.answer.call_args[0][0]


@pytest.mark.asyncio
@patch("src.bot.handlers.registration.send_dm_to_user", new_callable=AsyncMock)
async def test_slack_dm_failure_reports_error(mock_send: AsyncMock) -> None:
    mock_send.side_effect = SlackDeliveryError("not_in_channel")
    _awaiting_slack_id[42] = uuid4()
    msg = _fake_message(user_id=42)
    await _step_receive_slack_id(msg, 42, "U01234ABCDE")
    assert "Couldn't send" in msg.answer.call_args[0][0]
    assert 42 not in _pending  # did not advance


# ---------------------------------------------------------------------------
# _step_receive_code
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_wrong_code_rejected() -> None:
    uid = uuid4()
    _pending[42] = _PendingReg(
        internal_user_id=uid, slack_user_id="U01234ABCDE", code="AABB11"
    )
    msg = _fake_message(user_id=42, text="WRONGG")
    await _step_receive_code(msg, 42, "WRONGG")
    assert "doesn't match" in msg.answer.call_args[0][0]
    assert 42 in _pending  # stays pending


@pytest.mark.asyncio
async def test_expired_code_rejected() -> None:
    uid = uuid4()
    entry = _PendingReg(
        internal_user_id=uid, slack_user_id="U01234ABCDE", code="AABB11"
    )
    entry.expires_at = time.monotonic() - 1  # already expired
    _pending[42] = entry
    msg = _fake_message(user_id=42)
    await _step_receive_code(msg, 42, "AABB11")
    assert "expired" in msg.answer.call_args[0][0]
    assert 42 not in _pending


@pytest.mark.asyncio
async def test_correct_code_stores_slack_id() -> None:
    uid = uuid4()
    _pending[42] = _PendingReg(
        internal_user_id=uid, slack_user_id="U01234ABCDE", code="AABB11"
    )
    msg = _fake_message(user_id=42)

    fake_updated = MagicMock()
    fake_conn = AsyncMock()
    fake_txn = AsyncMock()
    fake_txn.__aenter__ = AsyncMock(return_value=fake_txn)
    fake_txn.__aexit__ = AsyncMock(return_value=False)
    fake_conn.transaction = MagicMock(return_value=fake_txn)

    with (
        patch("src.bot.handlers.registration.acquire_connection") as mock_acq,
        patch(
            "src.bot.handlers.registration.update_slack_user_id",
            new_callable=AsyncMock,
            return_value=fake_updated,
        ) as mock_update,
        patch(
            "src.bot.handlers.registration.insert_audit_log",
            new_callable=AsyncMock,
        ) as mock_audit,
    ):
        mock_acq.return_value.__aenter__ = AsyncMock(return_value=fake_conn)
        mock_acq.return_value.__aexit__ = AsyncMock(return_value=False)

        await _step_receive_code(msg, 42, "AABB11")

    mock_update.assert_called_once_with(fake_conn, uid, "U01234ABCDE")
    mock_audit.assert_called_once()
    assert 42 not in _pending
    assert "✅" in msg.answer.call_args[0][0]


@pytest.mark.asyncio
async def test_correct_code_case_insensitive() -> None:
    uid = uuid4()
    _pending[42] = _PendingReg(
        internal_user_id=uid, slack_user_id="U01234ABCDE", code="AABB11"
    )
    msg = _fake_message(user_id=42)

    fake_updated = MagicMock()
    fake_conn = AsyncMock()
    fake_txn = AsyncMock()
    fake_txn.__aenter__ = AsyncMock(return_value=fake_txn)
    fake_txn.__aexit__ = AsyncMock(return_value=False)
    fake_conn.transaction = MagicMock(return_value=fake_txn)

    with (
        patch("src.bot.handlers.registration.acquire_connection") as mock_acq,
        patch(
            "src.bot.handlers.registration.update_slack_user_id",
            new_callable=AsyncMock,
            return_value=fake_updated,
        ),
        patch(
            "src.bot.handlers.registration.insert_audit_log",
            new_callable=AsyncMock,
        ),
    ):
        mock_acq.return_value.__aenter__ = AsyncMock(return_value=fake_conn)
        mock_acq.return_value.__aexit__ = AsyncMock(return_value=False)

        await _step_receive_code(msg, 42, "aabb11")  # lowercase — should succeed

    assert 42 not in _pending
