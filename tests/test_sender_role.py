"""Tests for sender-role attribution in ingestion.

Role attribution and access control are different questions and must not share a
lookup. ``enabled`` is the access decision; being staff is a fact about who someone
is. Conflating them let a disabled internal manager's farewell message be analysed
as "Partner announces their last day at Beton.win", which fired five bogus
``partner_churn`` findings — the LLM reasoned correctly from a false premise.

No real DB: the query helper is monkeypatched, since the point under test is
*which* lookup ingestion chooses.
"""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any

import pytest

from src.db.models import InternalUser
from src.pipeline import ingest as ingest_mod


def _message(
    *, from_user: SimpleNamespace | None = None, sender_chat: SimpleNamespace | None = None,
    chat_id: int = -1001234567890,
) -> Any:
    """Minimal stand-in exposing only what ``_resolve_sender_role`` reads."""
    return SimpleNamespace(
        from_user=from_user,
        sender_chat=sender_chat,
        chat=SimpleNamespace(id=chat_id),
    )


def _user(tg_id: int, *, is_bot: bool = False) -> SimpleNamespace:
    return SimpleNamespace(id=tg_id, is_bot=is_bot)


@pytest.fixture
def staff_lookup(monkeypatch: pytest.MonkeyPatch) -> dict[int, InternalUser]:
    """Registry backing the enabled-agnostic lookup, keyed by Telegram id."""
    registry: dict[int, InternalUser] = {}

    async def _fake_any(conn: Any, telegram_user_id: int) -> InternalUser | None:
        return registry.get(telegram_user_id)

    monkeypatch.setattr(ingest_mod, "get_internal_user_by_telegram_id_any", _fake_any)
    return registry


async def test_registered_staff_is_internal(
    staff_lookup: dict[int, InternalUser], mk: Any
) -> None:
    staff_lookup[8422016171] = mk.user(tg_id=8422016171, name="Geralt | Betonwin")

    role = await ingest_mod._resolve_sender_role(None, _message(from_user=_user(8422016171)))

    assert role == "internal"


async def test_unregistered_sender_is_partner(staff_lookup: dict[int, InternalUser]) -> None:
    role = await ingest_mod._resolve_sender_role(None, _message(from_user=_user(5286641315)))

    assert role == "partner"


async def test_disabled_staff_stays_internal(
    staff_lookup: dict[int, InternalUser], mk: Any
) -> None:
    """The regression: an offboarded manager must not become a 'partner'.

    ``Сhicco| Betonwin`` announced their last day at Beton.win. Resolved through
    the enabled-only access gate they read as partner-side, so the finding became
    "partner churn" instead of an internal departure.
    """
    staff_lookup[7884114267] = mk.user(
        tg_id=7884114267, name="Сhicco| Betonwin", enabled=False
    )

    role = await ingest_mod._resolve_sender_role(None, _message(from_user=_user(7884114267)))

    assert role == "internal"


def test_ingestion_does_not_import_the_access_gate() -> None:
    """Guard the fix itself against a silent regression.

    If a later refactor reintroduces the enabled-only lookup here, roles would
    quietly go wrong again for disabled staff with nothing failing — the bug is
    invisible in behaviour until someone reads an LLM verdict and notices the
    premise is inverted. Pinning the module namespace makes that regression loud.
    """
    assert not hasattr(ingest_mod, "find_internal_user_by_telegram_id")
    assert hasattr(ingest_mod, "get_internal_user_by_telegram_id_any")


async def test_bots_are_unknown(staff_lookup: dict[int, InternalUser]) -> None:
    role = await ingest_mod._resolve_sender_role(
        None, _message(from_user=_user(777000, is_bot=True))
    )

    assert role == "unknown"


async def test_group_posting_as_itself_is_anonymous_admin(
    staff_lookup: dict[int, InternalUser],
) -> None:
    chat_id = -1001234567890
    role = await ingest_mod._resolve_sender_role(
        None, _message(sender_chat=SimpleNamespace(id=chat_id), chat_id=chat_id)
    )

    assert role == "anonymous_admin"


async def test_other_sender_chat_is_unknown(staff_lookup: dict[int, InternalUser]) -> None:
    role = await ingest_mod._resolve_sender_role(
        None,
        _message(sender_chat=SimpleNamespace(id=-100999), chat_id=-1001234567890),
    )

    assert role == "unknown"
