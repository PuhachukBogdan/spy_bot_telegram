"""Unit tests for DM-command argument parsing (Business-mode commands).

Just the pure parsers — the command handlers themselves are gated by the role
middleware and hit the DB, so they belong to the live-DB smoke pass.
"""

from __future__ import annotations

from src.bot.handlers.dm_commands import _parse_link_business_args


def test_link_args_plain() -> None:
    assert _parse_link_business_args("BC1 888 Acme") == ("BC1", 888, "Acme")


def test_link_args_quoted_name_with_spaces() -> None:
    assert _parse_link_business_args('BC1 888 "Acme Corp"') == ("BC1", 888, "Acme Corp")


def test_link_args_negative_id_allowed() -> None:
    # peer ids are positive, but the parser is generic and must accept ints
    assert _parse_link_business_args("BC1 -5 Name") == ("BC1", -5, "Name")


def test_link_args_missing_name_is_none() -> None:
    assert _parse_link_business_args("BC1 888") is None


def test_link_args_nonnumeric_peer_is_none() -> None:
    assert _parse_link_business_args("BC1 notanint Name") is None


def test_link_args_empty_is_none() -> None:
    assert _parse_link_business_args("") is None
    assert _parse_link_business_args(None) is None
