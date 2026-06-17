"""Unit tests for chat_member onboarding helpers."""

from __future__ import annotations

import pytest

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
