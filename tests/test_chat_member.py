"""Unit tests for chat_member onboarding helpers."""

from __future__ import annotations

from src.bot.handlers.chat_member import _parse_chat_title


def test_parse_standard_format() -> None:
    assert _parse_chat_title("12345 | PartnerName | betonwin") == ("12345", "PartnerName")


def test_parse_dot_variant() -> None:
    assert _parse_chat_title("77777 | UnclePartner | Beton.Win") == ("77777", "UnclePartner")


def test_parse_case_insensitive() -> None:
    assert _parse_chat_title("42 | Acme Corp | BETONWIN") == ("42", "Acme Corp")


def test_parse_extra_spaces() -> None:
    assert _parse_chat_title("  99  |  My Partner  |  beton.win  ") == ("99", "My Partner")


def test_parse_partner_name_with_spaces() -> None:
    assert _parse_chat_title("101 | Some Long Name Here | betonwin") == (
        "101",
        "Some Long Name Here",
    )


def test_parse_affid_last() -> None:
    # aff_id in the trailing position, company token in the middle.
    assert _parse_chat_title("Montapartners | BetonWin | 66642") == ("66642", "Montapartners")


def test_parse_affid_last_spaced_name() -> None:
    assert _parse_chat_title("Space Partners | Betonwin | 59422") == ("59422", "Space Partners")


def test_parse_no_numeric_affid() -> None:
    # Brand present but no purely-numeric token → aff_id "", name is the remainder.
    assert _parse_chat_title("Just A Partner | Beton.Win") == ("", "Just A Partner")


def test_parse_no_brand_suffix() -> None:
    assert _parse_chat_title("12345 | PartnerName") is None


def test_parse_wrong_brand() -> None:
    assert _parse_chat_title("12345 | PartnerName | otherbrand") is None


def test_parse_empty() -> None:
    assert _parse_chat_title("") is None


def test_parse_none() -> None:
    assert _parse_chat_title(None) is None


def test_parse_only_pipes() -> None:
    assert _parse_chat_title("| |") is None


# --- archive history re-attachment -------------------------------------------

from typing import Any  # noqa: E402
from uuid import UUID, uuid4  # noqa: E402

import pytest  # noqa: E402

from src.bot.handlers import chat_member as cm_mod  # noqa: E402
from src.db.queries.archive import ArchiveUnit, AttachResult  # noqa: E402


def test_title_aff_ids_collects_every_id() -> None:
    """A chat can serve several affiliates; the archive may sit under any of them."""
    assert cm_mod._title_aff_ids("LEGENDS | Betonwin | 58329 | 71862 | 74849") == [
        "58329",
        "71862",
        "74849",
    ]


def test_title_aff_ids_dedupes_and_skips_short_numbers() -> None:
    assert cm_mod._title_aff_ids("78284 | (77106) | MetaForge | 78284") == ["78284", "77106"]
    # 3-digit tokens are not affiliate ids ("(312)" in a real title).
    assert cm_mod._title_aff_ids("81325 | AVN | Beton.win (312)") == ["81325"]


def test_title_aff_ids_empty() -> None:
    assert cm_mod._title_aff_ids(None) == []
    assert cm_mod._title_aff_ids("No ids here | Betonwin") == []


@pytest.fixture
def attach_spy(monkeypatch: pytest.MonkeyPatch) -> dict[str, Any]:
    """Stub the archive queries and record what onboarding asked them to do."""
    state: dict[str, Any] = {"unit": None, "attached": None, "audit": None, "raise": None}

    async def _find(conn: Any, aff_ids: list[str]) -> ArchiveUnit | None:
        state["asked_for"] = aff_ids
        if state["raise"] is not None:
            raise state["raise"]
        return state["unit"]

    async def _attach(conn: Any, *, source_chat_id: UUID, target_chat_id: UUID) -> AttachResult:
        state["attached"] = (source_chat_id, target_chat_id)
        return AttachResult(
            source_chat_id=source_chat_id,
            target_chat_id=target_chat_id,
            messages_moved=204,
            messages_left=3,
            events_moved=7,
        )

    async def _audit(conn: Any, **kwargs: Any) -> None:
        state["audit"] = kwargs

    monkeypatch.setattr(cm_mod, "find_archived_unit_for_aff_ids", _find)
    monkeypatch.setattr(cm_mod, "attach_archived_history", _attach)
    monkeypatch.setattr(cm_mod, "insert_audit_log", _audit)
    return state


async def test_attach_moves_history_and_audits(attach_spy: dict[str, Any]) -> None:
    unit_id, chat_id = uuid4(), uuid4()
    attach_spy["unit"] = ArchiveUnit(
        id=unit_id, chat_name="TraffSkulls | 59114", import_aff_id="59114", message_count=204
    )

    await cm_mod._attach_archive_history(
        None, "[11011] TraffSkulls | Betonwin | 59114", chat_id, 8422016171
    )

    assert attach_spy["asked_for"] == ["11011", "59114"]
    assert attach_spy["attached"] == (unit_id, chat_id)
    audit = attach_spy["audit"]
    assert audit["action"] == "archive_history_attached"
    assert audit["payload"]["messages_moved"] == 204
    assert audit["payload"]["messages_left"] == 3


async def test_attach_is_a_noop_without_an_archive(attach_spy: dict[str, Any]) -> None:
    attach_spy["unit"] = None

    await cm_mod._attach_archive_history(None, "New Partner | Betonwin | 90001", uuid4(), 1)

    assert attach_spy["attached"] is None
    assert attach_spy["audit"] is None


async def test_attach_skipped_when_the_title_has_no_aff_id(attach_spy: dict[str, Any]) -> None:
    await cm_mod._attach_archive_history(None, "Just A Partner | Beton.Win", uuid4(), 1)

    assert "asked_for" not in attach_spy


async def test_attach_failure_never_breaks_onboarding(attach_spy: dict[str, Any]) -> None:
    """Losing the backlog is recoverable; losing the onboarding is not.

    The merge is idempotent and can be re-run, so a DB error here is swallowed
    rather than aborting the chat registration that has already happened.
    """
    import asyncpg

    attach_spy["raise"] = asyncpg.PostgresError("connection reset")

    await cm_mod._attach_archive_history(None, "Partner | Betonwin | 59114", uuid4(), 1)

    assert attach_spy["attached"] is None
