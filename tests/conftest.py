"""Shared test fixtures.

The most important object here is :class:`FakeBot`. Telegram Business mode has a
HARD INVARIANT — the bot only ever READS; it must never send a message through a
business connection (no ``business_connection_id`` on any outbound call). FakeBot
enforces that mechanically: every send is recorded, and any send that carries a
``business_connection_id`` (or any unexpected ``send_*`` call) raises, so a
regression that makes the bot "reply" to a partner fails the suite immediately.

Handlers talk to Postgres through small query helpers that each take an
``asyncpg.Connection``. Unit tests never touch a real DB: ``deps`` (in
``test_business.py``) monkeypatches every query the handler calls. The model
builders on :class:`Make` produce real Pydantic row models so the handler reads
genuinely-typed attributes.
"""

from __future__ import annotations

from datetime import UTC, datetime
from types import SimpleNamespace
from typing import Any
from uuid import UUID, uuid4

import pytest

from src.db.models import (
    BusinessConnection,
    Chat,
    InternalUser,
    Message,
    PartnerContact,
)


class FakeBot:
    """Stand-in for aiogram ``Bot`` that enforces the read-only invariant.

    Only ``send_message`` (plain internal DM: ``chat_id`` + ``text``) is allowed.
    Passing a ``business_connection_id`` — or calling any other ``send_*`` /
    ``copy_*`` / ``forward_*`` / ``edit_*`` API — is a hard failure: in business
    mode the bot must stay silent toward partners.
    """

    def __init__(self) -> None:
        self.sent: list[tuple[int, str | None]] = []

    async def send_message(
        self, chat_id: int, text: str | None = None, **kwargs: Any
    ) -> SimpleNamespace:
        assert "business_connection_id" not in kwargs, (
            "READ-ONLY INVARIANT VIOLATED: send_message carried a "
            "business_connection_id (the bot replied through a business "
            "connection)"
        )
        self.sent.append((chat_id, text))
        return SimpleNamespace(message_id=len(self.sent))

    def __getattr__(self, name: str):
        if name.startswith(("send_", "copy_", "forward_", "edit_")):

            async def _forbidden(*args: Any, **kwargs: Any) -> None:
                raise AssertionError(
                    f"unexpected bot.{name}(...) call in business mode — the "
                    "bot must only DM internal staff via send_message"
                )

            return _forbidden
        raise AttributeError(name)

    @property
    def chat_ids(self) -> list[int]:
        return [cid for cid, _ in self.sent]

    @property
    def texts(self) -> str:
        return "\n".join(t or "" for _, t in self.sent)


# --- aiogram update stand-ins ------------------------------------------------
# Lightweight objects exposing exactly the attributes the business handlers read.
# Cheaper and clearer than constructing full aiogram models in every test.


class _Rights:
    def model_dump(self, mode: str = "json") -> dict[str, Any]:
        return {"can_read_messages": True}


class FakeBCUpdate:
    """Stand-in for ``aiogram.types.BusinessConnection`` (the system update)."""

    def __init__(
        self, bc_id: str, owner_id: int, is_enabled: bool, with_rights: bool
    ) -> None:
        self.id = bc_id
        self.user = SimpleNamespace(id=owner_id)
        self.is_enabled = is_enabled
        self.rights = _Rights() if with_rights else None
        self.date = datetime.now(UTC)

    def model_dump(
        self, mode: str = "json", exclude_none: bool = False
    ) -> dict[str, Any]:
        return {"id": self.id, "is_enabled": self.is_enabled}


class FakeBizMessage:
    """Stand-in for an incoming ``business_message``."""

    def __init__(
        self,
        bc_id: str | None,
        peer_id: int,
        first: str | None,
        last: str | None,
        username: str | None,
    ) -> None:
        self.business_connection_id = bc_id
        self.chat = SimpleNamespace(
            id=peer_id, first_name=first, last_name=last, username=username
        )


class FakeEdit:
    """Stand-in for an ``edited_business_message``."""

    def __init__(
        self,
        bc_id: str | None,
        peer_id: int,
        message_id: int,
        text: str | None,
        edit_date: int | None,
    ) -> None:
        self.business_connection_id = bc_id
        self.chat = SimpleNamespace(id=peer_id)
        self.message_id = message_id
        self.text = text
        self.caption = None
        self.edit_date = edit_date
        self.date = datetime.now(UTC)


class FakeDeleted:
    """Stand-in for ``deleted_business_messages``."""

    def __init__(self, bc_id: str, peer_id: int, ids: list[int]) -> None:
        self.business_connection_id = bc_id
        self.chat = SimpleNamespace(id=peer_id)
        self.message_ids = ids

    def model_dump(
        self, mode: str = "json", exclude_none: bool = False
    ) -> dict[str, Any]:
        return {"message_ids": self.message_ids}


class Make:
    """Factories for DB row models and aiogram update stand-ins."""

    # -- DB row models --------------------------------------------------------
    def user(
        self,
        role: str = "manager",
        tg_id: int | None = None,
        accounts: list[int] | None = None,
        enabled: bool = True,
        name: str = "User",
    ) -> InternalUser:
        if accounts is not None:
            accs = list(accounts)
        elif tg_id is not None:
            accs = [tg_id]
        else:
            accs = []
        return InternalUser(
            id=uuid4(),
            full_name=name,
            role=role,  # type: ignore[arg-type]
            telegram_accounts=accs,
            enabled=enabled,
            created_at=datetime.now(UTC),
        )

    def grant(
        self,
        status: str = "active",
        internal_user_id: UUID | None = None,
        owner: int = 999,
        bc_id: str = "BC1",
    ) -> BusinessConnection:
        return BusinessConnection(
            id=uuid4(),
            business_connection_id=bc_id,
            business_account_user_id=owner,
            internal_user_id=internal_user_id,
            status=status,  # type: ignore[arg-type]
            connected_at=datetime.now(UTC),
            created_at=datetime.now(UTC),
        )

    def chat(
        self,
        status: str = "active",
        unit_type: str = "business",
        business_connection_id: str | None = "BC1",
        telegram_chat_id: int = 777,
        partner_id: UUID | None = None,
    ) -> Chat:
        return Chat(
            id=uuid4(),
            telegram_chat_id=telegram_chat_id,
            unit_type=unit_type,  # type: ignore[arg-type]
            status=status,
            business_connection_id=business_connection_id,
            business_peer_user_id=telegram_chat_id,
            partner_id=partner_id,
            created_at=datetime.now(UTC),
        )

    def contact(self, partner_id: UUID | None = None, tg_id: int = 777) -> PartnerContact:
        return PartnerContact(
            id=uuid4(),
            partner_id=partner_id or uuid4(),
            telegram_user_id=tg_id,
            created_at=datetime.now(UTC),
        )

    def message_row(
        self,
        base_score: int = 0,
        text: str = "old",
        sender_role: str = "partner",
        tmid: int = 10,
        is_significant: bool = False,
        created_at: datetime | None = None,
    ) -> Message:
        now = datetime.now(UTC)
        return Message(
            id=uuid4(),
            telegram_message_id=tmid,
            chat_id=uuid4(),
            sender_role=sender_role,
            message_type="text",
            message_text=text,
            base_score=base_score,
            is_significant=is_significant,
            timestamp=now,
            created_at=created_at or now,
        )

    # -- aiogram update stand-ins --------------------------------------------
    def bc_update_obj(
        self,
        bc_id: str = "BC1",
        owner_id: int = 999,
        is_enabled: bool = True,
        with_rights: bool = True,
    ) -> FakeBCUpdate:
        return FakeBCUpdate(bc_id, owner_id, is_enabled, with_rights)

    def biz_message(
        self,
        bc_id: str | None = "BC1",
        peer_id: int = 777,
        first: str | None = "Peer",
        last: str | None = None,
        username: str | None = None,
    ) -> FakeBizMessage:
        return FakeBizMessage(bc_id, peer_id, first, last, username)

    def edit(
        self,
        bc_id: str | None = "BC1",
        peer_id: int = 777,
        message_id: int = 10,
        text: str | None = "new text",
        edit_date: int | None = None,
    ) -> FakeEdit:
        return FakeEdit(bc_id, peer_id, message_id, text, edit_date)

    def deleted(
        self, bc_id: str = "BC1", peer_id: int = 777, ids: list[int] | None = None
    ) -> FakeDeleted:
        return FakeDeleted(bc_id, peer_id, ids if ids is not None else [10])


@pytest.fixture
def bot() -> FakeBot:
    return FakeBot()


@pytest.fixture
def mk() -> Make:
    return Make()
