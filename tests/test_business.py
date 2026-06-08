"""Unit tests for Telegram Business read-only secretary mode.

Covers the four dispatcher observers in ``src/bot/handlers/business.py`` plus the
formatting helpers. Every Postgres query the handlers call is monkeypatched (see
the ``deps`` fixture); the only "live" collaborators are the real ``notify_*``
helpers driving a :class:`FakeBot`, which lets the suite enforce the read-only
invariant end-to-end: across every path the bot may only DM internal staff, never
send through a business connection.
"""

from __future__ import annotations

from contextlib import asynccontextmanager
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock

import pytest

import src.bot.handlers.business as biz
from src.config import settings


@pytest.fixture
def deps(monkeypatch: pytest.MonkeyPatch) -> SimpleNamespace:
    """Patch every DB/queue/ingest seam the business handlers touch.

    Returns a namespace of the installed ``AsyncMock``s so a test can set return
    values and assert calls. ``notify_admins`` / ``notify_internal_user`` are left
    REAL on purpose — they exercise the genuine outbound path against FakeBot.
    """

    @asynccontextmanager
    async def fake_acquire() -> Any:
        yield object()

    monkeypatch.setattr(biz, "acquire_connection", fake_acquire)

    m = SimpleNamespace()

    # business_connections queries
    m.bc_get = AsyncMock(return_value=None)
    m.bc_create = AsyncMock()
    m.bc_update = AsyncMock()
    monkeypatch.setattr(biz.bc_q, "get_by_connection_id", m.bc_get)
    monkeypatch.setattr(biz.bc_q, "create", m.bc_create)
    monkeypatch.setattr(biz.bc_q, "update_status", m.bc_update)

    # internal_user lookups
    m.find_user = AsyncMock(return_value=None)
    m.get_user_by_id = AsyncMock(return_value=None)
    m.list_admins = AsyncMock(return_value=[])
    monkeypatch.setattr(biz, "find_internal_user_by_telegram_id", m.find_user)
    monkeypatch.setattr(biz, "get_internal_user_by_id", m.get_user_by_id)
    monkeypatch.setattr(biz, "list_admin_users", m.list_admins)

    # chats queries
    m.get_unit = AsyncMock(return_value=None)
    m.create_chat = AsyncMock(return_value=None)
    monkeypatch.setattr(biz, "get_by_unit", m.get_unit)
    monkeypatch.setattr(biz, "create_business_chat", m.create_chat)

    # partner_contacts
    m.pc_get = AsyncMock(return_value=None)
    monkeypatch.setattr(biz.pc_q, "get_by_telegram_user_id", m.pc_get)

    # audit / ingest / queue
    m.audit = AsyncMock()
    m.ingest = AsyncMock()
    m.enqueue = AsyncMock()
    m.bump = AsyncMock()
    monkeypatch.setattr(biz, "insert_audit_log", m.audit)
    monkeypatch.setattr(biz, "ingest_message", m.ingest)
    monkeypatch.setattr(biz, "enqueue_chat_analysis", m.enqueue)
    monkeypatch.setattr(biz, "bump_message_for_analysis", m.bump)

    # messages queries
    m.get_message = AsyncMock(return_value=None)
    m.insert_edit = AsyncMock()
    m.update_text = AsyncMock()
    m.update_triggers = AsyncMock()
    m.mark_deleted = AsyncMock(return_value=1)
    monkeypatch.setattr(biz, "get_message", m.get_message)
    monkeypatch.setattr(biz, "insert_message_edit", m.insert_edit)
    monkeypatch.setattr(biz, "update_message_text", m.update_text)
    monkeypatch.setattr(biz, "update_message_triggers", m.update_triggers)
    monkeypatch.setattr(biz, "mark_message_deleted", m.mark_deleted)

    # Tier-1 pattern matcher (process-wide singleton)
    m.match_result = SimpleNamespace(
        has_triggers=False, base_score=0, triggered_patterns={}
    )
    monkeypatch.setattr(
        biz, "pattern_cache", SimpleNamespace(match=lambda text, role: m.match_result)
    )

    return m


# =============================================================================
# business_connection — grant classification
# =============================================================================
async def test_connection_admin_auto_active(deps, bot, mk) -> None:
    admin = mk.user(role="admin", tg_id=999)
    deps.find_user.return_value = admin
    deps.bc_create.return_value = mk.grant(status="active", internal_user_id=admin.id)

    await biz.on_business_connection(mk.bc_update_obj("BC1", 999, True), bot)

    row = deps.bc_create.call_args.args[1]
    assert row.status == "active"
    assert row.approved_by == admin.id
    deps.bc_update.assert_not_awaited()
    assert bot.sent == []  # auto-active grants do NOT pester admins


async def test_connection_internal_nonadmin_pending_notifies_admins(deps, bot, mk) -> None:
    manager = mk.user(role="manager", tg_id=500)
    admin = mk.user(role="admin", tg_id=999, accounts=[999])
    deps.find_user.return_value = manager
    deps.bc_create.return_value = mk.grant(status="pending", internal_user_id=manager.id)
    deps.list_admins.return_value = [admin]

    await biz.on_business_connection(mk.bc_update_obj("BC2", 500, True), bot)

    row = deps.bc_create.call_args.args[1]
    assert row.status == "pending"
    assert row.approved_by is None
    assert 999 in bot.chat_ids
    assert "/approve_business BC2" in bot.texts


async def test_connection_external_owner_pending_notifies_admins(deps, bot, mk) -> None:
    admin = mk.user(role="admin", tg_id=999, accounts=[999])
    deps.find_user.return_value = None  # not an internal user at all
    deps.bc_create.return_value = mk.grant(status="pending", internal_user_id=None)
    deps.list_admins.return_value = [admin]

    await biz.on_business_connection(mk.bc_update_obj("BC3", 4242, True), bot)

    row = deps.bc_create.call_args.args[1]
    assert row.status == "pending"
    assert row.internal_user_id is None
    assert "EXTERNAL" in bot.texts
    assert 999 in bot.chat_ids


async def test_connection_disabled_marks_revoked(deps, bot, mk) -> None:
    deps.bc_get.return_value = mk.grant(status="active")

    await biz.on_business_connection(mk.bc_update_obj("BC1", 999, is_enabled=False), bot)

    # existing grant updated, never re-created
    deps.bc_create.assert_not_awaited()
    deps.bc_update.assert_awaited_once()
    assert deps.bc_update.call_args.args[2] == "revoked"


async def test_connection_existing_enabled_refresh_keeps_status(deps, bot, mk) -> None:
    deps.bc_get.return_value = mk.grant(status="pending")

    await biz.on_business_connection(mk.bc_update_obj("BC1", 999, is_enabled=True), bot)

    deps.bc_create.assert_not_awaited()
    deps.bc_update.assert_awaited_once()
    assert deps.bc_update.call_args.args[2] == "pending"  # status preserved
    assert bot.sent == []


# =============================================================================
# business_message — gating + linking (read-only)
# =============================================================================
async def test_message_dropped_when_connection_not_active(deps, bot, mk) -> None:
    deps.bc_get.return_value = mk.grant(status="pending")

    await biz.on_business_message(mk.biz_message("BC1", 777), bot)

    deps.ingest.assert_not_awaited()
    deps.create_chat.assert_not_awaited()
    assert bot.sent == []


async def test_message_known_contact_auto_links_and_ingests(deps, bot, mk) -> None:
    from uuid import uuid4

    deps.bc_get.return_value = mk.grant(status="active", internal_user_id=uuid4())
    deps.get_unit.return_value = None
    deps.pc_get.return_value = mk.contact(tg_id=777)
    deps.create_chat.return_value = mk.chat(status="active")

    await biz.on_business_message(mk.biz_message("BC1", 777), bot)

    assert deps.create_chat.call_args.kwargs["status"] == "active"
    deps.ingest.assert_awaited_once()
    assert bot.sent == []  # known contact: no owner prompt


async def test_message_unknown_contact_parks_pending_and_dms_owner(deps, bot, mk) -> None:
    owner = mk.user(role="manager", tg_id=500, accounts=[500])
    deps.bc_get.return_value = mk.grant(status="active", internal_user_id=owner.id)
    deps.get_unit.return_value = None
    deps.pc_get.return_value = None
    deps.create_chat.return_value = mk.chat(status="pending")
    deps.get_user_by_id.return_value = owner

    await biz.on_business_message(mk.biz_message("BC1", 888), bot)

    assert deps.create_chat.call_args.kwargs["status"] == "pending"
    deps.ingest.assert_not_awaited()  # nothing stored until linked
    assert 500 in bot.chat_ids
    assert "/link_business_chat BC1 888" in bot.texts


async def test_message_active_business_unit_ingests_without_recreating(deps, bot, mk) -> None:
    deps.bc_get.return_value = mk.grant(status="active")
    deps.get_unit.return_value = mk.chat(
        status="active", unit_type="business", business_connection_id="BC1"
    )

    await biz.on_business_message(mk.biz_message("BC1", 777), bot)

    deps.create_chat.assert_not_awaited()
    deps.ingest.assert_awaited_once()
    assert bot.sent == []


async def test_message_pending_unit_is_dropped(deps, bot, mk) -> None:
    deps.bc_get.return_value = mk.grant(status="active")
    deps.get_unit.return_value = mk.chat(
        status="pending", unit_type="business", business_connection_id="BC1"
    )

    await biz.on_business_message(mk.biz_message("BC1", 777), bot)

    deps.ingest.assert_not_awaited()
    deps.create_chat.assert_not_awaited()


async def test_message_unit_type_mismatch_is_dropped(deps, bot, mk) -> None:
    deps.bc_get.return_value = mk.grant(status="active")
    deps.get_unit.return_value = mk.chat(status="active", unit_type="group")

    await biz.on_business_message(mk.biz_message("BC1", 777), bot)

    deps.ingest.assert_not_awaited()


async def test_message_wrong_connection_for_unit_is_dropped(deps, bot, mk) -> None:
    deps.bc_get.return_value = mk.grant(status="active", bc_id="BC1")
    deps.get_unit.return_value = mk.chat(
        status="active", unit_type="business", business_connection_id="OTHER"
    )

    await biz.on_business_message(mk.biz_message("BC1", 777), bot)

    deps.ingest.assert_not_awaited()


# =============================================================================
# edited_business_message
# =============================================================================
async def test_edit_records_and_reruns_tier1(deps, bot, mk) -> None:
    deps.bc_get.return_value = mk.grant(status="active")
    deps.get_unit.return_value = mk.chat(
        status="active", unit_type="business", business_connection_id="BC1"
    )
    thr = settings.PRIORITY_SCORE_THRESHOLD
    deps.get_message.return_value = mk.message_row(base_score=max(0, thr - 1), text="old")
    deps.match_result.base_score = thr
    deps.match_result.has_triggers = thr > 0

    await biz.on_edited_business_message(mk.edit("BC1", 777, message_id=10, text="new"))

    deps.insert_edit.assert_awaited_once()
    deps.update_text.assert_awaited_once()
    deps.update_triggers.assert_awaited_once()
    if thr > 0:
        deps.enqueue.assert_awaited_once()  # crossed the priority threshold
    else:
        deps.enqueue.assert_not_awaited()


async def test_edit_noop_when_text_unchanged(deps, bot, mk) -> None:
    deps.bc_get.return_value = mk.grant(status="active")
    deps.get_unit.return_value = mk.chat(
        status="active", unit_type="business", business_connection_id="BC1"
    )
    deps.get_message.return_value = mk.message_row(text="same")

    await biz.on_edited_business_message(mk.edit("BC1", 777, message_id=10, text="same"))

    deps.insert_edit.assert_not_awaited()
    deps.update_text.assert_not_awaited()


async def test_edit_unstored_message_ignored(deps, bot, mk) -> None:
    deps.bc_get.return_value = mk.grant(status="active")
    deps.get_unit.return_value = mk.chat(
        status="active", unit_type="business", business_connection_id="BC1"
    )
    deps.get_message.return_value = None  # never stored

    await biz.on_edited_business_message(mk.edit("BC1", 777, message_id=99, text="new"))

    deps.insert_edit.assert_not_awaited()


async def test_edit_dropped_when_connection_inactive(deps, bot, mk) -> None:
    deps.bc_get.return_value = mk.grant(status="revoked")

    await biz.on_edited_business_message(mk.edit("BC1", 777, message_id=10, text="new"))

    deps.get_message.assert_not_awaited()
    deps.insert_edit.assert_not_awaited()


# =============================================================================
# deleted_business_messages
# =============================================================================
async def test_delete_soft_deletes_each_message(deps, bot, mk) -> None:
    deps.bc_get.return_value = mk.grant(status="active")
    deps.get_unit.return_value = mk.chat(
        status="active", unit_type="business", business_connection_id="BC1"
    )

    await biz.on_deleted_business_messages(mk.deleted("BC1", 777, ids=[10, 11, 12]))

    assert deps.mark_deleted.await_count == 3  # one soft-delete per id
    deps.audit.assert_awaited_once()


async def test_delete_dropped_when_connection_inactive(deps, bot, mk) -> None:
    deps.bc_get.return_value = mk.grant(status="revoked")

    await biz.on_deleted_business_messages(mk.deleted("BC1", 777, ids=[10]))

    deps.mark_deleted.assert_not_awaited()
    deps.audit.assert_not_awaited()


# =============================================================================
# Read-only invariant — the guard itself + a representative live path
# =============================================================================
async def test_fakebot_rejects_business_connection_send(bot) -> None:
    """Sanity: the invariant guard actually fires."""
    with pytest.raises(AssertionError):
        await bot.send_message(123, "leak", business_connection_id="BC1")


async def test_ingest_path_never_messages_partner(deps, bot, mk) -> None:
    """Even on the fully-active ingest path, the bot must stay silent outward."""
    deps.bc_get.return_value = mk.grant(status="active")
    deps.get_unit.return_value = mk.chat(
        status="active", unit_type="business", business_connection_id="BC1"
    )

    await biz.on_business_message(mk.biz_message("BC1", 777), bot)

    deps.ingest.assert_awaited_once()
    assert bot.sent == []  # zero outbound toward the partner peer


# =============================================================================
# Formatting helpers (pure functions)
# =============================================================================
def test_peer_label_prefers_full_name(mk) -> None:
    msg = mk.biz_message(first="Иван", last="Петров", username="ivan")
    assert biz._peer_label(msg) == "Иван Петров"


def test_peer_label_falls_back_to_username(mk) -> None:
    msg = mk.biz_message(first=None, last=None, username="ivan")
    assert biz._peer_label(msg) == "@ivan"


def test_peer_label_none_when_anonymous(mk) -> None:
    msg = mk.biz_message(first=None, last=None, username=None)
    assert biz._peer_label(msg) is None


def test_connection_notice_external_escapes_and_offers_actions() -> None:
    text = biz._connection_notice("B<C>", 4242, None)
    assert "EXTERNAL" in text
    assert "B&lt;C&gt;" in text  # bc_id HTML-escaped
    assert "/approve_business" in text and "/reject_business" in text


def test_link_prompt_includes_peer_and_command(mk) -> None:
    msg = mk.biz_message(first="Peer", username="peer", peer_id=888)
    text = biz._link_prompt("BC1", 888, msg)
    assert "888" in text
    assert "/link_business_chat BC1 888" in text
