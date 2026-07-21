"""Tests for the daily digest (query + day resolver). The digest is a web view
(first dashboard tab); there is no Telegram /daily command. No real DB."""

from __future__ import annotations

from datetime import UTC, date, datetime
from typing import Any

from src.db.queries.daily import DailyDigest, get_daily_digest, resolve_digest_day

_TODAY = date(2026, 7, 21)


# ---------------------------------------------------------------------------
# resolve_digest_day
# ---------------------------------------------------------------------------


def test_default_is_yesterday() -> None:
    day, err = resolve_digest_day(None, _TODAY)
    assert err is None
    assert day == date(2026, 7, 20)


def test_yesterday_keyword() -> None:
    assert resolve_digest_day("yesterday", _TODAY) == (date(2026, 7, 20), None)


def test_today_keyword() -> None:
    assert resolve_digest_day("today", _TODAY) == (_TODAY, None)


def test_explicit_iso_date() -> None:
    assert resolve_digest_day("2026-07-15", _TODAY) == (date(2026, 7, 15), None)


def test_bad_arg_rejected() -> None:
    day, err = resolve_digest_day("last-tuesday", _TODAY)
    assert day is None
    assert err is not None and "Invalid date" in err


def test_future_date_rejected() -> None:
    day, err = resolve_digest_day("2026-07-22", _TODAY)
    assert day is None
    assert err == "That date is in the future."


def test_older_than_30_days_rejected() -> None:
    day, err = resolve_digest_day("2026-06-01", _TODAY)
    assert day is None
    assert err is not None and "30 days" in err


def test_exactly_30_days_allowed() -> None:
    day, err = resolve_digest_day("2026-06-21", _TODAY)
    assert err is None
    assert day == date(2026, 6, 21)


# ---------------------------------------------------------------------------
# DailyDigest.has_activity
# ---------------------------------------------------------------------------


def _digest(**over: Any) -> DailyDigest:
    base = dict(
        messages_total=1247, significant=342, active_chats=34, total_active_chats=159,
        active_managers=3, risk_low=12, risk_medium=4, risk_high=1, risk_critical=0,
        new_chats=2, new_partners=1,
        active_chat_rows=[("77777 | Acme | Beton.Win", 89), ("Other | BW", 5)],
    )
    base.update(over)
    return DailyDigest(**base)  # type: ignore[arg-type]


def test_has_activity_flag() -> None:
    assert _digest().has_activity is True
    empty = _digest(
        messages_total=0, significant=0, new_chats=0, new_partners=0,
        risk_low=0, risk_medium=0, risk_high=0, risk_critical=0,
    )
    assert empty.has_activity is False


# ---------------------------------------------------------------------------
# get_daily_digest — canned fake connection
# ---------------------------------------------------------------------------


class _FakeConn:
    """Returns canned values by matching distinctive SQL substrings."""

    async def fetchval(self, sql: str, *args: Any) -> int:
        s = " ".join(sql.split())
        if "is_significant" in s:
            return 342
        if "COUNT(DISTINCT chat_id)" in s:
            return 34
        if "COUNT(DISTINCT c.authorized_by)" in s:
            return 3
        if "FROM messages WHERE created_at" in s:
            return 1247
        if "FROM chats WHERE status = 'active'" in s and "authorized_at" not in s:
            return 159
        if "FROM chats" in s and "authorized_at" in s:
            return 2
        if "FROM partners" in s:
            return 1
        raise AssertionError(f"unexpected fetchval SQL: {s}")

    async def fetch(self, sql: str, *args: Any) -> list[dict[str, Any]]:
        s = " ".join(sql.split())
        if "risk_level" in s:
            return [
                {"risk_level": "low", "c": 12},
                {"risk_level": "high", "c": 1},
                {"risk_level": "critical", "c": 0},
            ]
        # active-chats list
        return [
            {"id": "a", "chat_name": "77777 | Acme | Beton.Win", "n": 89},
            {"id": "b", "chat_name": "Other | BW", "n": 5},
        ]


async def test_get_daily_digest_maps_all_fields() -> None:
    start = datetime(2026, 5, 15, tzinfo=UTC)
    end = datetime(2026, 5, 16, tzinfo=UTC)
    d = await get_daily_digest(_FakeConn(), start, end)  # type: ignore[arg-type]
    assert d.messages_total == 1247
    assert d.significant == 342
    assert d.active_chats == 2  # derived from the active-chats list length
    assert d.total_active_chats == 159
    assert d.active_managers == 3
    assert (d.risk_low, d.risk_high, d.risk_critical) == (12, 1, 0)
    assert d.risk_medium == 0
    assert d.new_chats == 2
    assert d.new_partners == 1
    assert d.active_chat_rows == [("77777 | Acme | Beton.Win", 89), ("Other | BW", 5)]
    assert d.has_activity is True


async def test_get_daily_digest_no_active_chats() -> None:
    class _Empty(_FakeConn):
        async def fetch(self, sql: str, *args: Any) -> list[dict[str, Any]]:
            s = " ".join(sql.split())
            if "risk_level" in s:
                return []
            return []

    d = await get_daily_digest(
        _Empty(),  # type: ignore[arg-type]
        datetime(2026, 5, 15, tzinfo=UTC),
        datetime(2026, 5, 16, tzinfo=UTC),
    )
    assert d.active_chat_rows == []
    assert d.active_chats == 0
