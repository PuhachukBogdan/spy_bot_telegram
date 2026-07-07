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
from src.alerts.dedup import resolve_open_case_ts
from src.alerts.slack import SlackDeliveryError, build_alert_blocks
from src.db.models import Chat, RiskEvent
from src.utils.text import short_why
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
    # Find the context block that contains the /risk review link (not the
    # reviewed-by line that may be appended later; actions block may follow it).
    ctx = next(b for b in blocks if b["type"] == "context")
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


def test_blocks_show_message_date_not_verdict() -> None:
    event = _event(level="high")
    msg_dt = datetime(2026, 6, 5, 14, 32, tzinfo=UTC)
    blocks, _ = build_alert_blocks(
        event, _chat("Acme chat"), "Acme Corp", message_dt=msg_dt
    )
    flat = str(blocks)
    assert "*Verdict:*" not in flat                # verdict field dropped
    assert "*Date:*" in flat                       # replaced by the message date
    assert "2026-06-05 14:32 UTC" in flat          # the flagged message's send time
    # Falls back to detection time only when the message row is unavailable.
    blocks2, _ = build_alert_blocks(event, _chat("Acme chat"), "Acme Corp")
    assert event.created_at.strftime("%Y-%m-%d %H:%M UTC") in str(blocks2)


def test_short_why_trims_to_two_sentences_and_caps() -> None:
    assert short_why(None) == ""
    assert short_why("One only.") == "One only."
    assert short_why("First. Second. Third dropped.") == "First. Second."
    long = short_why("word " * 100)
    assert long.endswith("…") and len(long) <= 221


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


# --- resolve_open_case_ts (case policy) --------------------------------------


class _FetchvalConn:
    def __init__(self, value: str | None) -> None:
        self._value = value
        self.calls: list[tuple[Any, ...]] = []

    async def fetchval(self, query: str, *args: Any) -> str | None:
        self.calls.append(args)
        return self._value


async def test_open_case_returns_recent_alert_ts() -> None:
    conn = _FetchvalConn("1700.0001")
    chat_id = uuid4()
    ts = await resolve_open_case_ts(
        conn,  # type: ignore[arg-type]
        chat_id=chat_id,
        risk_type="private_channel",
    )
    assert ts == "1700.0001"
    # Queried with (chat_id, risk_type, since-cutoff) — critical included, no bypass.
    assert conn.calls[0][0] == chat_id
    assert conn.calls[0][1] == "private_channel"


async def test_no_open_case_returns_none() -> None:
    conn = _FetchvalConn(None)
    ts = await resolve_open_case_ts(
        conn,  # type: ignore[arg-type]
        chat_id=uuid4(),
        risk_type="private_channel",
    )
    assert ts is None


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
    """Patch dispatch's collaborators; return recorders for assertions.

    ``case_ts`` controls whether an open case exists (None → fresh card, a ts →
    update in place). ``case_events`` is what ``list_case_events`` returns when a
    case is updated. ``post_raises`` / ``update_raises`` simulate Slack failures.
    """
    rec: dict[str, Any] = {
        "posts": [],
        "updates": [],
        "ts_writes": [],
        "failures": [],
        "case_events": [],
    }

    monkeypatch.setattr(dispatch_mod, "acquire_connection", lambda: _NullAcquire())

    async def fake_resolve(conn: Any, **kw: Any) -> str | None:
        return rec.get("case_ts")

    async def fake_mentions(conn: Any) -> str:
        return rec.get("mention_prefix", "")

    async def fake_list(conn: Any, **kw: Any) -> list[Any]:
        return rec["case_events"]

    async def fake_post(
        *, channel: str, text: str, blocks: Any, thread_ts: str | None = None
    ) -> str:
        rec["posts"].append({"channel": channel, "thread_ts": thread_ts, "text": text})
        if rec.get("post_raises"):
            raise SlackDeliveryError("channel_not_found")
        return "1700000000.000100"

    async def fake_update(*, channel: str, ts: str, text: str, blocks: Any) -> None:
        rec["updates"].append({"channel": channel, "ts": ts, "text": text})
        if rec.get("update_raises"):
            raise SlackDeliveryError("edit_failed")

    async def fake_set(conn: Any, rid: Any, ts: str) -> None:
        rec["ts_writes"].append((rid, ts))

    async def fake_failed(bot: Any, event: Any, channel: str, error: str) -> None:
        rec["failures"].append((event.id, channel, error))

    monkeypatch.setattr(dispatch_mod, "resolve_open_case_ts", fake_resolve)
    monkeypatch.setattr(dispatch_mod, "critical_mention_prefix", fake_mentions)
    monkeypatch.setattr(dispatch_mod, "list_case_events", fake_list)
    monkeypatch.setattr(dispatch_mod, "post_alert", fake_post)
    monkeypatch.setattr(dispatch_mod, "update_alert", fake_update)
    monkeypatch.setattr(dispatch_mod, "set_slack_message_ts", fake_set)
    monkeypatch.setattr(dispatch_mod, "handle_failed_alert", fake_failed)
    monkeypatch.setattr(dispatch_mod.settings, "SLACK_CHANNEL_ALERTS", "#alerts")
    return rec


async def test_fresh_case_posts_top_level_and_writes_ts(
    patched_dispatch: dict[str, Any],
) -> None:
    patched_dispatch["case_ts"] = None  # no open case
    event = _event(level="high")
    await dispatch_mod.dispatch_alerts(FakeBot(), _chat("c"), "Acme", [event])  # type: ignore[arg-type]
    assert len(patched_dispatch["posts"]) == 1
    assert patched_dispatch["posts"][0]["channel"] == "#alerts"
    assert patched_dispatch["posts"][0]["thread_ts"] is None  # top-level
    assert patched_dispatch["updates"] == []
    assert patched_dispatch["ts_writes"] == [(event.id, "1700000000.000100")]
    assert patched_dispatch["failures"] == []


async def test_open_case_updates_card_in_place_no_new_post(
    patched_dispatch: dict[str, Any],
) -> None:
    patched_dispatch["case_ts"] = "1699999999.000001"
    event = _event(level="high")
    patched_dispatch["case_events"] = [event]  # single-signal case, no escalation
    await dispatch_mod.dispatch_alerts(FakeBot(), _chat("c"), "Acme", [event])  # type: ignore[arg-type]
    assert len(patched_dispatch["updates"]) == 1
    assert patched_dispatch["updates"][0]["ts"] == "1699999999.000001"
    assert patched_dispatch["posts"] == []  # no second alert
    # The finding joins the existing case card.
    assert patched_dispatch["ts_writes"] == [(event.id, "1699999999.000001")]


async def test_case_escalation_into_critical_pings_in_thread(
    patched_dispatch: dict[str, Any],
) -> None:
    patched_dispatch["case_ts"] = "1699999999.000001"
    patched_dispatch["mention_prefix"] = "<@U1> "
    prior = _event(level="high", score=72)
    event = _event(level="critical", score=90)
    patched_dispatch["case_events"] = [prior, event]  # case had no critical before
    await dispatch_mod.dispatch_alerts(FakeBot(), _chat("c"), "Acme", [event])  # type: ignore[arg-type]
    assert len(patched_dispatch["updates"]) == 1  # card edited in place
    assert len(patched_dispatch["posts"]) == 1  # one threaded re-ping
    ping = patched_dispatch["posts"][0]
    assert ping["thread_ts"] == "1699999999.000001"
    assert ping["text"].startswith("<@U1> ")
    assert patched_dispatch["ts_writes"] == [(event.id, "1699999999.000001")]


async def test_already_critical_case_does_not_reping(
    patched_dispatch: dict[str, Any],
) -> None:
    patched_dispatch["case_ts"] = "1699999999.000001"
    patched_dispatch["mention_prefix"] = "<@U1> "
    prior = _event(level="critical", score=85)
    event = _event(level="critical", score=90)
    patched_dispatch["case_events"] = [prior, event]  # already critical → no new ping
    await dispatch_mod.dispatch_alerts(FakeBot(), _chat("c"), "Acme", [event])  # type: ignore[arg-type]
    assert len(patched_dispatch["updates"]) == 1
    assert patched_dispatch["posts"] == []


async def test_fresh_case_delivery_failure_records_and_skips_ts_write(
    patched_dispatch: dict[str, Any],
) -> None:
    patched_dispatch["case_ts"] = None
    patched_dispatch["post_raises"] = True
    event = _event(level="high")
    await dispatch_mod.dispatch_alerts(FakeBot(), _chat("c"), "Acme", [event])  # type: ignore[arg-type]
    assert patched_dispatch["ts_writes"] == []  # never wrote a ts
    assert len(patched_dispatch["failures"]) == 1
    assert patched_dispatch["failures"][0][1] == "#alerts"


async def test_case_update_failure_records_and_skips_ts_write(
    patched_dispatch: dict[str, Any],
) -> None:
    patched_dispatch["case_ts"] = "1699999999.000001"
    patched_dispatch["update_raises"] = True
    event = _event(level="high")
    patched_dispatch["case_events"] = [event]
    await dispatch_mod.dispatch_alerts(FakeBot(), _chat("c"), "Acme", [event])  # type: ignore[arg-type]
    assert len(patched_dispatch["updates"]) == 1  # attempted
    assert patched_dispatch["ts_writes"] == []  # never wrote a ts
    assert len(patched_dispatch["failures"]) == 1
