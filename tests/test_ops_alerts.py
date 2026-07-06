"""Tests for the ops-alerts subsystem (payment incidents + Argentina holidays).

No network or DB: the feed parser and holiday calendar are pure; the workers run
with monkeypatched state / DB / Telegram collaborators. A local OpsFakeBot is
used instead of conftest.FakeBot because ops alerts is the sanctioned
proactive-write path — it both sends to groups AND edits messages, which the
business-mode FakeBot deliberately forbids.
"""

from __future__ import annotations

from datetime import UTC, date, datetime, timedelta
from types import SimpleNamespace
from typing import Any
from uuid import uuid4

import httpx
import pytest
from aiogram.exceptions import TelegramAPIError
from tenacity import RetryError

from src.pipeline.ops_alerts import holidays_worker as hw
from src.pipeline.ops_alerts import incidents_worker as iw
from src.pipeline.ops_alerts import tg_sender
from src.pipeline.ops_alerts.feed_parser import (
    Incident,
    extract_latest_status,
    fetch_incidents,
    parse_incidents,
)
from src.pipeline.ops_alerts.holidays_calendar import (
    find_tomorrow_holiday,
    get_easter_date,
    get_holidays,
)
from src.pipeline.ops_alerts.templates import (
    format_holiday,
    format_new_incident,
    format_update,
)

# ---------------------------------------------------------------------------
# Local fakes
# ---------------------------------------------------------------------------


class OpsFakeBot:
    """Records sends + edits; can inject failures by chat id."""

    def __init__(self, fail_send: set[int] | None = None,
                 edit_error: str | None = None) -> None:
        self.sent: list[tuple[int, str | None]] = []
        self.edited: list[tuple[int, int, str | None]] = []
        self._fail_send = fail_send or set()
        self._edit_error = edit_error

    async def send_message(self, chat_id: int, text: str | None = None,
                           **kwargs: Any) -> SimpleNamespace:
        if chat_id in self._fail_send:
            raise TelegramAPIError(method=SimpleNamespace(), message="send boom")
        self.sent.append((chat_id, text))
        return SimpleNamespace(message_id=1000 + len(self.sent))

    async def edit_message_text(self, text: str, *, chat_id: int,
                                message_id: int, **kwargs: Any) -> SimpleNamespace:
        if self._edit_error is not None:
            raise TelegramAPIError(method=SimpleNamespace(), message=self._edit_error)
        self.edited.append((chat_id, message_id, text))
        return SimpleNamespace(message_id=message_id)


class _NullConn:
    async def __aenter__(self) -> None:
        return None

    async def __aexit__(self, *exc: object) -> bool:
        return False


def _group(tg_id: int) -> SimpleNamespace:
    return SimpleNamespace(id=uuid4(), telegram_chat_id=tg_id)


def _incident(incident_id: str = "INC1", status: str = "In progress") -> Incident:
    return Incident(
        incident_id=incident_id,
        country="Chile",
        provider="Webpay",
        issue="Timeouts",
        link=f"https://x/incidents/{incident_id}",
        details="line1\nIn progress - foo",
        status=status,
        iso_date=datetime(2026, 6, 24, 12, 0, tzinfo=UTC),
    )


# ---------------------------------------------------------------------------
# feed_parser
# ---------------------------------------------------------------------------


def test_extract_latest_status_in_progress() -> None:
    assert extract_latest_status("<p><strong>In progress</strong> - 12:00</p>") == "In progress"


def test_extract_latest_status_resolved() -> None:
    # Newest update is the first <strong> in the summary HTML; older ones follow.
    summary = (
        "<p><small>Jul 2, 20:46 UTC</small><br /> <strong>Resolved</strong> - done</p>"
        "<p><small>Jul 2, 20:36 UTC</small><br /> <strong>Monitoring</strong> - watching</p>"
    )
    assert extract_latest_status(summary) == "Resolved"


def test_extract_latest_status_unknown() -> None:
    assert extract_latest_status("no status here") == "Unknown"


def test_parse_incidents_keeps_target_provider() -> None:
    entries = [{
        "title": "Chile - Webpay - Timeouts",
        "link": "https://x/incidents/abc",
        "summary": "<p><strong>In progress</strong> - 12:00</p>",
    }]
    out = parse_incidents(entries)
    assert len(out) == 1
    assert out[0].country == "Chile"
    assert out[0].provider == "Webpay"
    assert out[0].incident_id == "abc"
    assert out[0].status == "In progress"


def test_parse_incidents_drops_other_country() -> None:
    entries = [{
        "title": "Brazil - PIX - Down",
        "link": "https://x/incidents/zzz",
        "summary": "\nIn progress - 12:00",
    }]
    assert parse_incidents(entries) == []


def test_parse_incidents_drops_unmatched_provider() -> None:
    entries = [{
        "title": "Chile - SomeRandomBank - Down",
        "link": "https://x/incidents/zzz",
        "summary": "\nIn progress - 12:00",
    }]
    assert parse_incidents(entries) == []


def test_parse_incidents_substring_match() -> None:
    # "MercadoPago Chile" contains target "MercadoPago".
    entries = [{
        "title": "Chile - MercadoPago Chile - Slow",
        "link": "https://x/incidents/q1",
        "summary": "\nIn progress - 12:00",
    }]
    assert len(parse_incidents(entries)) == 1


def test_parse_incidents_skips_malformed_title() -> None:
    entries = [{"title": "garbage", "link": "https://x/1", "summary": ""}]
    assert parse_incidents(entries) == []


def test_incident_is_resolved_flag() -> None:
    assert _incident(status="Resolved").is_resolved is True
    assert _incident(status="In progress").is_resolved is False


# ---------------------------------------------------------------------------
# holidays_calendar
# ---------------------------------------------------------------------------


def test_easter_known_dates() -> None:
    assert get_easter_date(2024) == date(2024, 3, 31)
    assert get_easter_date(2025) == date(2025, 4, 20)
    assert get_easter_date(2026) == date(2026, 4, 5)


def test_holidays_count_is_18() -> None:
    assert len(get_holidays(2026)) == 18


def test_find_tomorrow_holiday_hit() -> None:
    # 8 July → tomorrow 9 July is Día de la Independencia.
    h = find_tomorrow_holiday(date(2026, 7, 8))
    assert h is not None
    assert "Independencia" in h.name


def test_find_tomorrow_holiday_miss() -> None:
    assert find_tomorrow_holiday(date(2026, 7, 15)) is None


def test_find_tomorrow_holiday_year_boundary() -> None:
    # 31 Dec → tomorrow 1 Jan is Año Nuevo (next year).
    h = find_tomorrow_holiday(date(2026, 12, 31))
    assert h is not None and h.name == "Año Nuevo"


# ---------------------------------------------------------------------------
# templates — HTML escaping
# ---------------------------------------------------------------------------


def test_templates_escape_html() -> None:
    inc = Incident(
        incident_id="i1", country="Chile", provider="Webpay",
        issue="<b>x</b>", link="https://x/i1", details="a & b <script>alert(1)</script>",
        status="In progress", iso_date=None,
    )
    new = format_new_incident(inc, detected_at="now")
    upd = format_update(inc, updated_at="now")
    # The issue field is escaped — never rendered as live HTML.
    assert "<b>x</b>" not in new and "&lt;b&gt;x&lt;/b&gt;" in new
    # details HTML is STRIPPED (not escaped-and-shown): no raw tag survives, but
    # the surrounding text is preserved with its ampersand re-escaped.
    assert "<script>" not in new and "<script>" not in upd
    assert "&lt;script&gt;" not in upd  # tag stripped, not rendered as text
    assert "&amp; b" in upd


def test_incident_templates_do_not_leak_source() -> None:
    """The feed's status-page URL / incident id must never reach a partner group."""
    inc = Incident(
        incident_id="kcl0h1z62jjs", country="Chile", provider="Santander",
        issue="Decrease in Conversion Rates",
        link="https://status.d24.com/incidents/kcl0h1z62jjs",
        details='<p>Resolved - see <a href="https://status.d24.com/x">status</a></p>',
        status="Resolved", iso_date=None,
    )
    new = format_new_incident(inc, detected_at="now")
    upd = format_update(inc, updated_at="now")
    for out in (new, upd):
        assert "d24" not in out.lower()        # no source-provider name
        assert "http" not in out.lower()       # no link rendered at all
        assert inc.incident_id not in out      # no internal incident id
        assert "santander" not in out.lower()  # payment provider name never shown
        assert "provider" not in out.lower()   # no Provider: field at all


def test_format_update_resolved_header() -> None:
    upd = format_update(_incident(status="Resolved"), updated_at="now")
    assert "RESOLVED" in upd
    upd2 = format_update(_incident(status="In progress"), updated_at="now")
    assert "UPDATE" in upd2


def test_format_holiday_has_name() -> None:
    h = get_holidays(2026)[0]  # Año Nuevo
    out = format_holiday(h)
    assert "Año Nuevo" in out
    assert "Population celebrating" not in out  # dropped from the header


# ---------------------------------------------------------------------------
# tg_sender
# ---------------------------------------------------------------------------


async def test_broadcast_isolates_failure() -> None:
    bot = OpsFakeBot(fail_send={222})
    targets = [(uuid4(), 111), (uuid4(), 222), (uuid4(), 333)]
    sent = await tg_sender.broadcast(bot, targets, "hi", concurrency=5)  # type: ignore[arg-type]
    assert len(sent) == 2  # 222 failed, other two delivered
    assert {c for c, _ in bot.sent} == {111, 333}


async def test_edit_not_modified_is_ok() -> None:
    bot = OpsFakeBot(edit_error="Bad Request: message is not modified")
    cid = uuid4()
    items = [{"chat_id": cid, "telegram_chat_id": 111, "telegram_message_id": 5}]
    results = await tg_sender.edit_messages(bot, items, "x", concurrency=5)  # type: ignore[arg-type]
    assert results[0].ok is True


async def test_edit_real_failure_reported() -> None:
    bot = OpsFakeBot(edit_error="Bad Request: message to edit not found")
    cid = uuid4()
    items = [{"chat_id": cid, "telegram_chat_id": 111, "telegram_message_id": 5}]
    results = await tg_sender.edit_messages(bot, items, "x", concurrency=5)  # type: ignore[arg-type]
    assert results[0].ok is False
    assert results[0].reason is not None


# ---------------------------------------------------------------------------
# incidents_worker
# ---------------------------------------------------------------------------


@pytest.fixture
def iw_patch(monkeypatch: pytest.MonkeyPatch) -> dict[str, Any]:
    rec: dict[str, Any] = {
        "inserted": [], "updated": [], "recorded": [], "broadcast": 0,
        "edited": 0, "count": 1, "existing": None, "incidents": [],
        "groups": [_group(111), _group(222)], "messages": [],
    }

    monkeypatch.setattr(iw, "acquire_connection", lambda: _NullConn())

    async def fake_fetch(url: str, *, retries: int, retry_delay_seconds: int) -> list[Incident]:
        return rec["incidents"]

    async def fake_count(conn: Any) -> int:
        return rec["count"]

    async def fake_get(conn: Any, incident_id: str) -> dict[str, Any] | None:
        return rec["existing"]

    async def fake_insert(conn: Any, **kw: Any) -> None:
        rec["inserted"].append(kw)

    async def fake_update(conn: Any, **kw: Any) -> None:
        rec["updated"].append(kw)

    async def fake_record(conn: Any, **kw: Any) -> None:
        rec["recorded"].append(kw)

    async def fake_list_messages(conn: Any, incident_id: str) -> list[dict[str, Any]]:
        return rec["messages"]

    async def fake_groups(conn: Any) -> list[Any]:
        return rec["groups"]

    async def fake_broadcast(bot: Any, targets: Any, text: str, *, concurrency: int) -> list[Any]:
        rec["broadcast"] += 1
        return [tg_sender.SendResult(chat_id=u, telegram_message_id=1) for u, _ in targets]

    async def fake_edit(bot: Any, items: Any, text: str, *, concurrency: int) -> list[Any]:
        rec["edited"] += 1
        return [tg_sender.EditResult(chat_id=i["chat_id"], ok=True) for i in items]

    async def fake_mark_edited(conn: Any, **kw: Any) -> None:
        pass

    async def fake_mark_failed(conn: Any, **kw: Any) -> None:
        pass

    monkeypatch.setattr(iw, "fetch_incidents", fake_fetch)
    monkeypatch.setattr(iw.state, "count_incidents", fake_count)
    monkeypatch.setattr(iw.state, "get_incident", fake_get)
    monkeypatch.setattr(iw.state, "insert_incident", fake_insert)
    monkeypatch.setattr(iw.state, "update_incident", fake_update)
    monkeypatch.setattr(iw.state, "record_message", fake_record)
    monkeypatch.setattr(iw.state, "list_incident_messages", fake_list_messages)
    monkeypatch.setattr(iw.state, "mark_message_edited", fake_mark_edited)
    monkeypatch.setattr(iw.state, "mark_message_edit_failed", fake_mark_failed)
    monkeypatch.setattr(iw, "list_active_group_chats", fake_groups)
    monkeypatch.setattr(iw, "broadcast", fake_broadcast)
    monkeypatch.setattr(iw, "edit_messages", fake_edit)
    monkeypatch.setattr(iw.settings, "OPS_FEED_URL", SimpleNamespace(get_secret_value=lambda: "http://feed"))
    return rec


async def test_first_run_seeds_without_broadcast(iw_patch: dict[str, Any]) -> None:
    iw_patch["count"] = 0  # empty table → first run
    iw_patch["incidents"] = [_incident("A"), _incident("B")]
    await iw.run_incidents_tick(OpsFakeBot())  # type: ignore[arg-type]
    assert len(iw_patch["inserted"]) == 2
    assert all(i["seeded_only"] is True for i in iw_patch["inserted"])
    assert iw_patch["broadcast"] == 0  # nothing sent on first run


async def test_new_active_incident_is_pending_not_broadcast(iw_patch: dict[str, Any]) -> None:
    # A never-seen active incident is recorded but held for the broadcast delay —
    # no message goes out on first sight (a sub-hour dip may recover on its own).
    iw_patch["count"] = 5
    iw_patch["existing"] = None
    iw_patch["incidents"] = [_incident("NEW", status="In progress")]
    await iw.run_incidents_tick(OpsFakeBot())  # type: ignore[arg-type]
    assert len(iw_patch["inserted"]) == 1
    assert iw_patch["inserted"][0]["seeded_only"] is False
    assert iw_patch["broadcast"] == 0  # deferred, not sent yet
    assert iw_patch["recorded"] == []


async def test_pending_incident_broadcasts_after_delay(iw_patch: dict[str, Any]) -> None:
    # Still active past the delay → promoted to a live broadcast.
    iw_patch["count"] = 5
    iw_patch["existing"] = {
        "seeded_only": False,
        "created_at": datetime.now(UTC) - timedelta(hours=2),
    }
    iw_patch["messages"] = []  # pending: nothing posted yet
    iw_patch["incidents"] = [_incident("OLD", status="In progress")]
    await iw.run_incidents_tick(OpsFakeBot())  # type: ignore[arg-type]
    assert len(iw_patch["updated"]) == 1
    assert iw_patch["broadcast"] == 1
    assert len(iw_patch["recorded"]) == 2  # one per group


async def test_pending_incident_waits_inside_delay(iw_patch: dict[str, Any]) -> None:
    # Active but younger than the delay → keep waiting, nothing sent.
    iw_patch["count"] = 5
    iw_patch["existing"] = {
        "seeded_only": False,
        "created_at": datetime.now(UTC) - timedelta(minutes=10),
    }
    iw_patch["messages"] = []
    iw_patch["incidents"] = [_incident("YOUNG", status="In progress")]
    await iw.run_incidents_tick(OpsFakeBot())  # type: ignore[arg-type]
    assert len(iw_patch["updated"]) == 1
    assert iw_patch["broadcast"] == 0


async def test_pending_incident_recovered_in_window_is_never_sent(iw_patch: dict[str, Any]) -> None:
    # Resolved before the delay elapsed → silent, no broadcast (the whole point).
    iw_patch["count"] = 5
    iw_patch["existing"] = {
        "seeded_only": False,
        "created_at": datetime.now(UTC) - timedelta(minutes=10),
    }
    iw_patch["messages"] = []
    iw_patch["incidents"] = [_incident("GONE", status="Resolved")]
    await iw.run_incidents_tick(OpsFakeBot())  # type: ignore[arg-type]
    assert len(iw_patch["updated"]) == 1  # status refreshed
    assert iw_patch["broadcast"] == 0


async def test_unseen_resolved_incident_is_skipped(iw_patch: dict[str, Any]) -> None:
    iw_patch["count"] = 5
    iw_patch["existing"] = None
    iw_patch["incidents"] = [_incident("R", status="Resolved")]
    await iw.run_incidents_tick(OpsFakeBot())  # type: ignore[arg-type]
    assert iw_patch["inserted"] == []
    assert iw_patch["broadcast"] == 0


async def test_known_posted_incident_is_edited(iw_patch: dict[str, Any]) -> None:
    iw_patch["count"] = 5
    iw_patch["existing"] = {"seeded_only": False}
    iw_patch["messages"] = [
        {"chat_id": uuid4(), "telegram_chat_id": 111, "telegram_message_id": 9}
    ]
    iw_patch["incidents"] = [_incident("UPD", status="Resolved")]
    await iw.run_incidents_tick(OpsFakeBot())  # type: ignore[arg-type]
    assert len(iw_patch["updated"]) == 1
    assert iw_patch["edited"] == 1


async def test_seeded_incident_stays_silent_on_update(iw_patch: dict[str, Any]) -> None:
    iw_patch["count"] = 5
    iw_patch["existing"] = {"seeded_only": True}
    iw_patch["incidents"] = [_incident("S", status="Resolved")]
    await iw.run_incidents_tick(OpsFakeBot())  # type: ignore[arg-type]
    assert len(iw_patch["updated"]) == 1
    assert iw_patch["edited"] == 0  # no messages to edit — stays silent


# ---------------------------------------------------------------------------
# holidays_worker
# ---------------------------------------------------------------------------


class _FakeDT:
    """datetime stand-in returning a fixed local time at a chosen hour."""

    hour = 14

    @classmethod
    def now(cls, tz: Any = None) -> datetime:
        return datetime(2026, 7, 8, cls.hour, 0, tzinfo=tz)


@pytest.fixture
def hw_patch(monkeypatch: pytest.MonkeyPatch) -> dict[str, Any]:
    rec: dict[str, Any] = {"claimed": True, "broadcast": 0, "groups": [_group(111)]}
    monkeypatch.setattr(iw, "acquire_connection", lambda: _NullConn())
    monkeypatch.setattr(hw, "acquire_connection", lambda: _NullConn())
    monkeypatch.setattr(hw, "datetime", _FakeDT)
    monkeypatch.setattr(hw.settings, "OPS_HOLIDAYS_CRON_HOUR", 13)

    async def fake_claim(conn: Any, holiday_date: date, holiday_name: str) -> bool:
        return bool(rec["claimed"])

    async def fake_groups(conn: Any) -> list[Any]:
        return rec["groups"]

    async def fake_broadcast(bot: Any, targets: Any, text: str, *, concurrency: int) -> list[Any]:
        rec["broadcast"] += 1
        return []

    monkeypatch.setattr(hw.state, "record_holiday_sent", fake_claim)
    monkeypatch.setattr(hw, "list_active_group_chats", fake_groups)
    monkeypatch.setattr(hw, "broadcast", fake_broadcast)
    return rec


async def test_holiday_fires_after_hour(hw_patch: dict[str, Any]) -> None:
    _FakeDT.hour = 14  # >= 13
    fired = await hw.run_holidays_tick(OpsFakeBot())  # type: ignore[arg-type]
    assert fired is True
    assert hw_patch["broadcast"] == 1


async def test_holiday_skips_before_hour(hw_patch: dict[str, Any]) -> None:
    _FakeDT.hour = 10  # < 13 → too early
    fired = await hw.run_holidays_tick(OpsFakeBot())  # type: ignore[arg-type]
    assert fired is False
    assert hw_patch["broadcast"] == 0


async def test_holiday_dedup_when_already_claimed(hw_patch: dict[str, Any]) -> None:
    _FakeDT.hour = 14
    hw_patch["claimed"] = False  # another tick already claimed it
    fired = await hw.run_holidays_tick(OpsFakeBot())  # type: ignore[arg-type]
    assert fired is False
    assert hw_patch["broadcast"] == 0


# ---------------------------------------------------------------------------
# fetch_incidents — HTTP layer (redirect, retry, parse round-trip)
# ---------------------------------------------------------------------------

_SAMPLE_RSS = """\
<?xml version="1.0" encoding="UTF-8"?>
<rss version="2.0"><channel><title>D24</title>
  <item>
    <title>Chile - Webpay - Timeouts</title>
    <link>https://status.d24.com/incidents/abc123</link>
    <description>&#10;In progress - 12:00</description>
  </item>
</channel></rss>"""


async def test_fetch_incidents_parses_valid_rss(monkeypatch: pytest.MonkeyPatch) -> None:
    """fetch_incidents: valid RSS XML is fetched and parsed into Incident records."""

    class _Resp:
        text: str = _SAMPLE_RSS

        def raise_for_status(self) -> None:
            pass

    class _Client:
        def __init__(self, **kw: Any) -> None:
            pass

        async def __aenter__(self) -> _Client:
            return self

        async def __aexit__(self, *a: Any) -> None:
            pass

        async def get(self, url: str) -> _Resp:
            return _Resp()

    monkeypatch.setattr(httpx, "AsyncClient", _Client)
    result = await fetch_incidents("http://fake", retries=1, retry_delay_seconds=0)
    assert len(result) == 1
    assert result[0].incident_id == "abc123"
    assert result[0].country == "Chile"
    assert result[0].provider == "Webpay"


async def test_fetch_incidents_sets_follow_redirects(monkeypatch: pytest.MonkeyPatch) -> None:
    """AsyncClient must be constructed with follow_redirects=True (fixes 302 redirect)."""
    captured: dict[str, Any] = {}

    class _Resp:
        text: str = "<rss><channel/></rss>"

        def raise_for_status(self) -> None:
            pass

    class _Client:
        def __init__(self, **kw: Any) -> None:
            captured.update(kw)

        async def __aenter__(self) -> _Client:
            return self

        async def __aexit__(self, *a: Any) -> None:
            pass

        async def get(self, url: str) -> _Resp:
            return _Resp()

    monkeypatch.setattr(httpx, "AsyncClient", _Client)
    await fetch_incidents("http://fake", retries=1, retry_delay_seconds=0)
    assert captured.get("follow_redirects") is True


async def test_fetch_incidents_retries_then_succeeds(monkeypatch: pytest.MonkeyPatch) -> None:
    """fetch_incidents: retries after a transient failure, succeeds on the second attempt."""
    call_count = 0

    class _Resp:
        text: str = _SAMPLE_RSS

        def raise_for_status(self) -> None:
            pass

    class _Client:
        def __init__(self, **kw: Any) -> None:
            pass

        async def __aenter__(self) -> _Client:
            return self

        async def __aexit__(self, *a: Any) -> None:
            pass

        async def get(self, url: str) -> _Resp:
            nonlocal call_count
            call_count += 1
            if call_count == 1:
                raise OSError("transient network error")
            return _Resp()

    monkeypatch.setattr(httpx, "AsyncClient", _Client)
    result = await fetch_incidents("http://fake", retries=3, retry_delay_seconds=0)
    assert call_count == 2
    assert len(result) == 1


async def test_fetch_incidents_raises_on_retry_exhaustion(monkeypatch: pytest.MonkeyPatch) -> None:
    """fetch_incidents: raises RetryError when every retry attempt fails."""

    class _Client:
        def __init__(self, **kw: Any) -> None:
            pass

        async def __aenter__(self) -> _Client:
            return self

        async def __aexit__(self, *a: Any) -> None:
            pass

        async def get(self, url: str) -> Any:
            raise OSError("always fails")

    monkeypatch.setattr(httpx, "AsyncClient", _Client)
    with pytest.raises(RetryError):
        await fetch_incidents("http://fake", retries=2, retry_delay_seconds=0)
