"""Unit tests for the Phase 11 Slack alert layer.

No real Slack or DB: the Block Kit builder and cooldown policy are pure / fed a
tiny fake connection, and the dispatch orchestration monkeypatches its
collaborators (post_alert, the lookups, the ts write-back, the failure fallback)
so we assert routing and ordering without a network call.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any
from uuid import uuid4

import pytest

from src.alerts import dispatch as dispatch_mod
from src.alerts.critical import critical_mention_prefix
from src.alerts.dedup import resolve_thread_ts
from src.alerts.slack import SlackDeliveryError, build_alert_blocks
from src.db.models import Chat, RiskEvent
from tests.conftest import FakeBot

# --- helpers -----------------------------------------------------------------


def _event(level: str = "high", risk_type: str = "private_channel", score: int = 72) -> RiskEvent:
    return RiskEvent(
        id=uuid4(),
        chat_id=uuid4(),
        risk_type=risk_type,
        risk_level=level,
        base_score=0,
        final_score=score,
        detected_phrase="между нами",
        llm_explanation="settle off the books",
        llm_verdict="confirmed",
        created_at=datetime.now(UTC),
    )


def _chat(name: str | None = None) -> Chat:
    return Chat(
        id=uuid4(),
        telegram_chat_id=-1001234567890,
        chat_name=name,
        status="active",
        created_at=datetime.now(UTC),
    )


def _context_text(blocks: list[dict[str, Any]]) -> str:
    ctx = blocks[-1]
    assert ctx["type"] == "context"
    return str(ctx["elements"][0]["text"])


# --- build_alert_blocks (pure) -----------------------------------------------


def test_blocks_high_badge_and_review_link() -> None:
    event = _event(level="high")
    blocks, text = build_alert_blocks(event, _chat("Acme chat"), "Acme Corp")
    assert "HIGH RISK" in text
    assert "Acme Corp" in text
    assert f"/risk {str(event.id)[:8]}" in _context_text(blocks)
    # Risk type is humanised.
    assert any("Private Channel" in str(b) for b in blocks)


def test_blocks_critical_badge_and_mention_prefix() -> None:
    event = _event(level="critical", score=90)
    blocks, text = build_alert_blocks(
        event, _chat("Acme chat"), "Acme Corp", mention_prefix="<@U1> "
    )
    assert text.startswith("<@U1> ")
    assert "CRITICAL RISK" in text
    # The mention rides the first (header) block too, so Block Kit clients ping.
    assert "<@U1>" in str(blocks[0])


def test_blocks_fallbacks_when_partner_and_name_missing() -> None:
    event = _event()
    blocks, text = build_alert_blocks(event, _chat(None), None)
    # No partner -> em dash; no chat name -> "chat <id>".
    assert "—" in text or "—" in str(blocks)
    assert "chat -1001234567890" in str(blocks)


# --- resolve_thread_ts (cooldown policy) -------------------------------------


class _FetchvalConn:
    def __init__(self, value: str | None) -> None:
        self._value = value
        self.calls: list[tuple[Any, ...]] = []

    async def fetchval(self, query: str, *args: Any) -> str | None:
        self.calls.append(args)
        return self._value


async def test_critical_bypasses_cooldown_without_querying() -> None:
    conn = _FetchvalConn("1700.0001")
    ts = await resolve_thread_ts(
        conn,  # type: ignore[arg-type]
        chat_id=uuid4(),
        risk_type="private_channel",
        is_critical=True,
    )
    assert ts is None
    assert conn.calls == []  # critical never looks up a thread


async def test_non_critical_threads_under_recent_alert() -> None:
    conn = _FetchvalConn("1700.0001")
    chat_id = uuid4()
    ts = await resolve_thread_ts(
        conn,  # type: ignore[arg-type]
        chat_id=chat_id,
        risk_type="private_channel",
        is_critical=False,
    )
    assert ts == "1700.0001"
    # Queried with (chat_id, risk_type, since-cutoff).
    assert conn.calls[0][0] == chat_id
    assert conn.calls[0][1] == "private_channel"


# --- critical_mention_prefix -------------------------------------------------


class _FetchConn:
    def __init__(self, rows: list[dict[str, Any]]) -> None:
        self._rows = rows

    async def fetch(self, query: str, *args: Any) -> list[dict[str, Any]]:
        return self._rows


def _recipient_row(slack_user_id: str) -> dict[str, Any]:
    return {
        "id": uuid4(),
        "full_name": f"User {slack_user_id}",
        "slack_user_id": slack_user_id,
        "email": None,
        "enabled": True,
        "added_at": datetime.now(UTC),
    }


async def test_mention_prefix_joins_recipients() -> None:
    conn = _FetchConn([_recipient_row("U1"), _recipient_row("U2")])
    prefix = await critical_mention_prefix(conn)  # type: ignore[arg-type]
    assert prefix == "<@U1> <@U2> "


async def test_mention_prefix_empty_when_none() -> None:
    conn = _FetchConn([])
    assert await critical_mention_prefix(conn) == ""  # type: ignore[arg-type]


# --- dispatch orchestration --------------------------------------------------


class _NullAcquire:
    async def __aenter__(self) -> None:
        return None

    async def __aexit__(self, *exc: Any) -> bool:
        return False


@pytest.fixture
def patched_dispatch(monkeypatch: pytest.MonkeyPatch) -> dict[str, Any]:
    """Patch dispatch's collaborators; return recorders for assertions."""
    rec: dict[str, Any] = {"posts": [], "ts_writes": [], "failures": []}

    monkeypatch.setattr(dispatch_mod, "acquire_connection", lambda: _NullAcquire())

    async def fake_resolve(conn: Any, **kw: Any) -> str | None:
        return rec.get("thread_ts")

    async def fake_mentions(conn: Any) -> str:
        return rec.get("mention_prefix", "")

    async def fake_post(
        *, channel: str, text: str, blocks: Any, thread_ts: str | None = None
    ) -> str:
        rec["posts"].append({"channel": channel, "thread_ts": thread_ts})
        if rec.get("post_raises"):
            raise SlackDeliveryError("channel_not_found")
        return "1700000000.000100"

    async def fake_set(conn: Any, rid: Any, ts: str) -> None:
        rec["ts_writes"].append((rid, ts))

    async def fake_failed(bot: Any, event: Any, channel: str, error: str) -> None:
        rec["failures"].append((event.id, channel, error))

    monkeypatch.setattr(dispatch_mod, "resolve_thread_ts", fake_resolve)
    monkeypatch.setattr(dispatch_mod, "critical_mention_prefix", fake_mentions)
    monkeypatch.setattr(dispatch_mod, "post_alert", fake_post)
    monkeypatch.setattr(dispatch_mod, "set_slack_message_ts", fake_set)
    monkeypatch.setattr(dispatch_mod, "handle_failed_alert", fake_failed)
    monkeypatch.setattr(dispatch_mod.settings, "SLACK_CHANNEL_ALERTS", "#alerts")
    monkeypatch.setattr(dispatch_mod.settings, "SLACK_CHANNEL_CRITICAL", "#critical")
    return rec


async def test_high_alert_posts_to_alerts_channel_and_writes_ts(
    patched_dispatch: dict[str, Any],
) -> None:
    event = _event(level="high")
    await dispatch_mod.dispatch_alerts(FakeBot(), _chat("c"), "Acme", [event])  # type: ignore[arg-type]
    assert len(patched_dispatch["posts"]) == 1
    assert patched_dispatch["posts"][0]["channel"] == "#alerts"
    assert patched_dispatch["ts_writes"] == [(event.id, "1700000000.000100")]
    assert patched_dispatch["failures"] == []


async def test_critical_alert_pings_and_mirrors_to_main(
    patched_dispatch: dict[str, Any],
) -> None:
    patched_dispatch["mention_prefix"] = "<@U1> "
    event = _event(level="critical", score=90)
    await dispatch_mod.dispatch_alerts(FakeBot(), _chat("c"), "Acme", [event])  # type: ignore[arg-type]
    channels = [p["channel"] for p in patched_dispatch["posts"]]
    assert channels == ["#critical", "#alerts"]  # primary then mirror
    assert patched_dispatch["ts_writes"] == [(event.id, "1700000000.000100")]


async def test_threaded_repeat_passes_thread_ts(
    patched_dispatch: dict[str, Any],
) -> None:
    patched_dispatch["thread_ts"] = "1699999999.000001"
    event = _event(level="high")
    await dispatch_mod.dispatch_alerts(FakeBot(), _chat("c"), "Acme", [event])  # type: ignore[arg-type]
    assert patched_dispatch["posts"][0]["thread_ts"] == "1699999999.000001"


async def test_delivery_failure_records_and_skips_ts_write(
    patched_dispatch: dict[str, Any],
) -> None:
    patched_dispatch["post_raises"] = True
    event = _event(level="high")
    await dispatch_mod.dispatch_alerts(FakeBot(), _chat("c"), "Acme", [event])  # type: ignore[arg-type]
    assert patched_dispatch["ts_writes"] == []  # never wrote a ts
    assert len(patched_dispatch["failures"]) == 1
    assert patched_dispatch["failures"][0][1] == "#alerts"
