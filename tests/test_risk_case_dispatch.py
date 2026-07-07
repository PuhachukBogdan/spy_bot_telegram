"""Integration tests for the risk-case dispatch lifecycle (2026-06-27 rework).

Higher fidelity than the routing tests in ``test_alerts.py``: here the REAL
collaborators run — ``resolve_open_case_ts`` (+ ``find_recent_alert_ts``),
``list_case_events``, ``set_slack_message_ts`` and ``build_alert_blocks`` — against
an in-memory store that faithfully mimics the two SQL queries the feature relies
on. Only the Slack SDK client and the DB connection are faked, so these exercise
"one case = one card" end to end without a network or a real DB.

  * 2b — two same-type criticals across passes collapse into ONE card (the
    regression: critical used to bypass the cooldown and fire twice);
  * 2c — a high case escalating to critical edits the card and posts ONE threaded
    re-ping;
  * 2d — genuinely different risk types stay separate cards (no over-merge).
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Any
from uuid import uuid4

import pytest

from src.alerts import dispatch as dispatch_mod
from src.alerts import slack as slack_mod
from src.db.models import Chat, RiskEvent
from tests.conftest import FakeBot

CHAT = Chat(
    id=uuid4(),
    telegram_chat_id=-1001234567890,
    chat_name="Partner X",
    status="active",
    created_at=datetime.now(UTC),
)


def _event(risk_type: str, level: str, score: int, created: datetime) -> RiskEvent:
    """A persisted-but-undispatched risk event (slack_message_ts still None)."""
    return RiskEvent(
        id=uuid4(),
        chat_id=CHAT.id,
        risk_type=risk_type,
        risk_level=level,
        base_score=0,
        final_score=score,
        detected_phrase="а если попробуем обойти платёжку?",
        llm_explanation="settle off the official payment rail",
        llm_verdict="confirmed",
        slack_message_ts=None,
        created_at=created,
    )


class _FakeSlack:
    """Captures chat.postMessage / chat.update; hands out incrementing ts values."""

    def __init__(self) -> None:
        self.posts: list[dict[str, Any]] = []
        self.updates: list[dict[str, Any]] = []
        self._n = 0

    async def chat_postMessage(self, **kw: Any) -> dict[str, Any]:  # noqa: N802
        self._n += 1
        self.posts.append(kw)
        return {"ts": f"17000000{self._n:02d}.0001"}

    async def chat_update(self, **kw: Any) -> dict[str, Any]:  # noqa: N802
        self.updates.append(kw)
        return {"ok": True}


class _FakeConn:
    """Mimics find_recent_alert_ts / list_case_events / set_slack_message_ts SQL."""

    def __init__(self, store: list[RiskEvent]) -> None:
        self.store = store

    async def fetchval(self, query: str, *args: Any) -> str | None:  # find_recent_alert_ts
        chat_id, risk_type, since = args
        rows = [
            r
            for r in self.store
            if r.chat_id == chat_id
            and r.risk_type == risk_type
            and r.slack_message_ts is not None
            and r.created_at >= since
        ]
        rows.sort(key=lambda r: r.created_at, reverse=True)
        return rows[0].slack_message_ts if rows else None

    async def fetch(self, query: str, *args: Any) -> list[dict[str, Any]]:  # list_case_events
        chat_id, risk_type, since = args
        rows = [
            r
            for r in self.store
            if r.chat_id == chat_id and r.risk_type == risk_type and r.created_at >= since
        ]
        rows.sort(key=lambda r: r.created_at)
        return [r.model_dump() for r in rows]

    async def execute(self, query: str, *args: Any) -> None:  # set_slack_message_ts
        rid, ts = args
        for i, r in enumerate(self.store):
            if r.id == rid:
                self.store[i] = r.model_copy(update={"slack_message_ts": ts})


class _FakeAcquire:
    def __init__(self, conn: _FakeConn) -> None:
        self.conn = conn

    async def __aenter__(self) -> _FakeConn:
        return self.conn

    async def __aexit__(self, *exc: Any) -> bool:
        return False


@pytest.fixture
def case_env(monkeypatch: pytest.MonkeyPatch) -> tuple[list[RiskEvent], _FakeSlack]:
    """Wire the real dispatch path to an in-memory store + fake Slack client."""
    store: list[RiskEvent] = []
    slack = _FakeSlack()
    conn = _FakeConn(store)

    monkeypatch.setattr(dispatch_mod, "acquire_connection", lambda: _FakeAcquire(conn))
    monkeypatch.setattr(slack_mod, "get_slack_client", lambda: slack)
    monkeypatch.setattr(dispatch_mod.settings, "SLACK_CHANNEL_ALERTS", "#alerts")

    async def _prefix(_conn: Any) -> str:
        return "<@U_ONCALL> "

    async def _failed(bot: Any, event: Any, channel: str, error: str) -> None:
        raise AssertionError(f"unexpected delivery-failure fallback: {error}")

    async def _no_suppressions(_conn: Any) -> list[Any]:
        return []

    monkeypatch.setattr(dispatch_mod, "critical_mention_prefix", _prefix)
    monkeypatch.setattr(dispatch_mod, "handle_failed_alert", _failed)
    monkeypatch.setattr(dispatch_mod, "list_active_suppressions", _no_suppressions)
    return store, slack


async def test_same_type_criticals_collapse_to_one_card(
    case_env: tuple[list[RiskEvent], _FakeSlack],
) -> None:
    """2b — two criticals of one type across passes = one card, edited in place."""
    store, slack = case_env
    now = datetime.now(UTC)
    ev1 = _event("hidden_payment", "critical", 100, now - timedelta(minutes=2))
    store.append(ev1)
    await dispatch_mod.dispatch_alerts(FakeBot(), CHAT, "Partner X", [ev1])  # type: ignore[arg-type]

    ev2 = _event("hidden_payment", "critical", 100, now)  # next analysis pass
    store.append(ev2)
    await dispatch_mod.dispatch_alerts(FakeBot(), CHAT, "Partner X", [ev2])  # type: ignore[arg-type]

    assert len(slack.posts) == 1  # one top-level card, NOT two alerts
    assert len(slack.updates) == 1  # second finding edited the card
    assert "Case: 2 signals" in str(slack.updates[0]["blocks"])
    assert not any(p.get("thread_ts") for p in slack.posts)  # already critical → no re-ping
    assert store[1].slack_message_ts == store[0].slack_message_ts  # joined the case card


async def test_high_case_escalation_into_critical_repings(
    case_env: tuple[list[RiskEvent], _FakeSlack],
) -> None:
    """2c — high case escalating to critical edits the card + posts one threaded ping."""
    store, slack = case_env
    now = datetime.now(UTC)
    ev1 = _event("hidden_payment", "high", 72, now - timedelta(minutes=2))
    store.append(ev1)
    await dispatch_mod.dispatch_alerts(FakeBot(), CHAT, "Partner X", [ev1])  # type: ignore[arg-type]

    ev2 = _event("hidden_payment", "critical", 96, now)
    store.append(ev2)
    await dispatch_mod.dispatch_alerts(FakeBot(), CHAT, "Partner X", [ev2])  # type: ignore[arg-type]

    assert len(slack.updates) == 1  # card edited in place
    assert len(slack.posts) == 2  # original card + one threaded re-ping
    ping = slack.posts[1]
    assert ping["thread_ts"] == store[0].slack_message_ts  # threaded under the case
    assert ping["text"].startswith("<@U_ONCALL> ")  # re-pings on-call
    assert "CRITICAL" in ping["text"]


async def test_distinct_risk_types_stay_separate(
    case_env: tuple[list[RiskEvent], _FakeSlack],
) -> None:
    """2d — different risk types are different cases (no over-merge)."""
    store, slack = case_env
    now = datetime.now(UTC)
    ev1 = _event("hidden_payment", "critical", 100, now - timedelta(minutes=1))
    store.append(ev1)
    await dispatch_mod.dispatch_alerts(FakeBot(), CHAT, "Partner X", [ev1])  # type: ignore[arg-type]

    ev2 = _event("traffic_leakage", "high", 70, now)  # unrelated concern
    store.append(ev2)
    await dispatch_mod.dispatch_alerts(FakeBot(), CHAT, "Partner X", [ev2])  # type: ignore[arg-type]

    assert len(slack.posts) == 2  # two distinct top-level cards
    assert len(slack.updates) == 0  # neither edited the other
