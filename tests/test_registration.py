"""Tests for the /register Slack OTP flow (registration.py)."""

from __future__ import annotations

import time
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


def _fake_message(user_id: int = 1001, text: str = "", full_name: str = "Test User") -> MagicMock:
    msg = MagicMock()
    msg.from_user = MagicMock()
    msg.from_user.id = user_id
    msg.from_user.full_name = full_name
    msg.text = text
    msg.answer = AsyncMock()
    return msg


def _fake_internal_user(slack_user_id: str | None = None) -> MagicMock:
    user = MagicMock()
    user.id = uuid4()
    user.slack_user_id = slack_user_id
    return user


def _patch_register_db(existing: Any = None) -> Any:
    """Patch acquire_connection + find_internal_user_by_telegram_id for cmd_register."""
    fake_conn = AsyncMock()

    from contextlib import asynccontextmanager
    from collections.abc import AsyncIterator

    @asynccontextmanager
    async def _fake_acquire() -> AsyncIterator[Any]:
        yield fake_conn

    return patch.multiple(
        "src.bot.handlers.registration",
        acquire_connection=_fake_acquire,
        find_internal_user_by_telegram_id=AsyncMock(return_value=existing),
    )


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
    msg = _fake_message(user_id=42, full_name="Alice")
    with _patch_register_db(existing=None):
        await reg_mod.cmd_register(msg)
    assert 42 in _awaiting_slack_id
    assert _awaiting_slack_id[42] == "Alice"
    msg.answer.assert_called_once()
    assert "member ID" in msg.answer.call_args[0][0]


@pytest.mark.asyncio
async def test_register_shows_relink_note_when_already_linked() -> None:
    msg = _fake_message(user_id=42)
    existing = _fake_internal_user(slack_user_id="U0EXISTING0")
    with _patch_register_db(existing=existing):
        await reg_mod.cmd_register(msg)
    assert "replacing" in msg.answer.call_args[0][0]


@pytest.mark.asyncio
async def test_register_no_relink_note_when_not_linked() -> None:
    msg = _fake_message(user_id=42)
    existing = _fake_internal_user(slack_user_id=None)
    with _patch_register_db(existing=existing):
        await reg_mod.cmd_register(msg)
    assert "replacing" not in msg.answer.call_args[0][0]


@pytest.mark.asyncio
async def test_register_clears_existing_pending_state() -> None:
    _pending[42] = _PendingReg(tg_full_name="Old", slack_user_id="U0OLDDDDDD", code="AABBCC")
    msg = _fake_message(user_id=42)
    with _patch_register_db(existing=None):
        await reg_mod.cmd_register(msg)
    assert 42 not in _pending
    assert 42 in _awaiting_slack_id


# ---------------------------------------------------------------------------
# /cancel command
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_cancel_clears_awaiting_state() -> None:
    _awaiting_slack_id[42] = "Alice"
    msg = _fake_message(user_id=42)
    await reg_mod.cmd_cancel(msg)
    assert 42 not in _awaiting_slack_id
    msg.answer.assert_called_once()
    assert "cancelled" in msg.answer.call_args[0][0].lower()


@pytest.mark.asyncio
async def test_cancel_clears_pending_state() -> None:
    _pending[42] = _PendingReg(tg_full_name="Alice", slack_user_id="U0ABCDEFGH", code="AA1122")
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
    _awaiting_slack_id[42] = "Alice"
    msg = _fake_message(user_id=42)
    await _step_receive_slack_id(msg, 42, "notanid")
    msg.answer.assert_called_once()
    assert "doesn't look" in msg.answer.call_args[0][0]
    assert 42 in _awaiting_slack_id  # stays in awaiting state


@pytest.mark.asyncio
async def test_slack_id_too_short_rejected() -> None:
    _awaiting_slack_id[42] = "Alice"
    msg = _fake_message(user_id=42)
    await _step_receive_slack_id(msg, 42, "U0123")  # too short
    assert "doesn't look" in msg.answer.call_args[0][0]


@pytest.mark.asyncio
@patch("src.bot.handlers.registration.send_dm_to_user", new_callable=AsyncMock)
async def test_valid_slack_id_sends_otp(mock_send: AsyncMock) -> None:
    _awaiting_slack_id[42] = "Alice"
    msg = _fake_message(user_id=42)

    await _step_receive_slack_id(msg, 42, "U01234ABCDE")

    mock_send.assert_called_once()
    assert mock_send.call_args[0][0] == "U01234ABCDE"
    assert 42 not in _awaiting_slack_id
    assert 42 in _pending
    assert _pending[42].slack_user_id == "U01234ABCDE"
    assert _pending[42].tg_full_name == "Alice"
    assert len(_pending[42].code) == 6
    msg.answer.assert_called_once()
    assert "6-character" in msg.answer.call_args[0][0]


@pytest.mark.asyncio
@patch("src.bot.handlers.registration.send_dm_to_user", new_callable=AsyncMock)
async def test_slack_dm_failure_reports_error(mock_send: AsyncMock) -> None:
    mock_send.side_effect = SlackDeliveryError("not_in_channel")
    _awaiting_slack_id[42] = "Alice"
    msg = _fake_message(user_id=42)
    await _step_receive_slack_id(msg, 42, "U01234ABCDE")
    assert "Couldn't send" in msg.answer.call_args[0][0]
    assert 42 not in _pending  # did not advance


# ---------------------------------------------------------------------------
# _step_receive_code — existing user path
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_wrong_code_rejected() -> None:
    _pending[42] = _PendingReg(tg_full_name="Alice", slack_user_id="U01234ABCDE", code="AABB11")
    msg = _fake_message(user_id=42, text="WRONGG")
    await _step_receive_code(msg, 42, "WRONGG")
    assert "doesn't match" in msg.answer.call_args[0][0]
    assert 42 in _pending  # stays pending


@pytest.mark.asyncio
async def test_expired_code_rejected() -> None:
    entry = _PendingReg(tg_full_name="Alice", slack_user_id="U01234ABCDE", code="AABB11")
    entry.expires_at = time.monotonic() - 1  # already expired
    _pending[42] = entry
    msg = _fake_message(user_id=42)
    await _step_receive_code(msg, 42, "AABB11")
    assert "expired" in msg.answer.call_args[0][0]
    assert 42 not in _pending


@pytest.mark.asyncio
async def test_correct_code_stores_slack_id_existing_user() -> None:
    """Correct OTP for a user already in internal_users: update slack_user_id."""
    existing = _fake_internal_user()
    _pending[42] = _PendingReg(tg_full_name="Alice", slack_user_id="U01234ABCDE", code="AABB11")
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
            "src.bot.handlers.registration.find_internal_user_by_telegram_id",
            new_callable=AsyncMock,
            return_value=existing,
        ),
        patch(
            "src.bot.handlers.registration.create_internal_user",
            new_callable=AsyncMock,
        ) as mock_create,
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

    mock_create.assert_not_called()
    mock_update.assert_called_once_with(fake_conn, existing.id, "U01234ABCDE")
    mock_audit.assert_called_once()
    assert 42 not in _pending
    assert "✅" in msg.answer.call_args[0][0]


@pytest.mark.asyncio
async def test_correct_code_creates_manager_when_new_user() -> None:
    """Correct OTP for an unknown user: create manager record, then link Slack."""
    new_user = _fake_internal_user()
    _pending[42] = _PendingReg(tg_full_name="Alice", slack_user_id="U01234ABCDE", code="AABB11")
    msg = _fake_message(user_id=42, full_name="Alice")

    fake_updated = MagicMock()
    fake_conn = AsyncMock()
    fake_txn = AsyncMock()
    fake_txn.__aenter__ = AsyncMock(return_value=fake_txn)
    fake_txn.__aexit__ = AsyncMock(return_value=False)
    fake_conn.transaction = MagicMock(return_value=fake_txn)

    with (
        patch("src.bot.handlers.registration.acquire_connection") as mock_acq,
        patch(
            "src.bot.handlers.registration.find_internal_user_by_telegram_id",
            new_callable=AsyncMock,
            return_value=None,  # not yet in DB
        ),
        patch(
            "src.bot.handlers.registration.create_internal_user",
            new_callable=AsyncMock,
            return_value=new_user,
        ) as mock_create,
        patch(
            "src.bot.handlers.registration.update_slack_user_id",
            new_callable=AsyncMock,
            return_value=fake_updated,
        ) as mock_update,
        patch(
            "src.bot.handlers.registration.insert_audit_log",
            new_callable=AsyncMock,
        ),
    ):
        mock_acq.return_value.__aenter__ = AsyncMock(return_value=fake_conn)
        mock_acq.return_value.__aexit__ = AsyncMock(return_value=False)

        await _step_receive_code(msg, 42, "AABB11")

    mock_create.assert_called_once_with(
        fake_conn, full_name="Alice", telegram_id=42, role="manager"
    )
    mock_update.assert_called_once_with(fake_conn, new_user.id, "U01234ABCDE")
    assert 42 not in _pending
    assert "✅" in msg.answer.call_args[0][0]


@pytest.mark.asyncio
async def test_correct_code_case_insensitive() -> None:
    existing = _fake_internal_user()
    _pending[42] = _PendingReg(tg_full_name="Alice", slack_user_id="U01234ABCDE", code="AABB11")
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
            "src.bot.handlers.registration.find_internal_user_by_telegram_id",
            new_callable=AsyncMock,
            return_value=existing,
        ),
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
