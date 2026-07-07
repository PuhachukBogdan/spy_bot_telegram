"""Tests for the failed-alert retry worker (idea #16).

No Slack or DB: ``_retry_one_failed_alert`` runs with its collaborators
monkeypatched. Covers the three outcomes — re-delivered, still-failing, and
orphaned/already-delivered.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any
from unittest.mock import AsyncMock, MagicMock
from uuid import uuid4

from src.alerts.slack import SlackDeliveryError
from src.db.models import Chat, FailedAlert, RiskEvent
from src.pipeline import workers as workers_mod


class _NullAcquire:
    async def __aenter__(self) -> None:
        return None

    async def __aexit__(self, *exc: Any) -> bool:
        return False


def _event(slack_ts: str | None = None) -> RiskEvent:
    return RiskEvent(
        id=uuid4(),
        chat_id=uuid4(),
        risk_type="private_channel",
        risk_level="high",
        base_score=0,
        final_score=72,
        detected_phrase="между нами",
        llm_explanation="settle off the books",
        slack_message_ts=slack_ts,
        created_at=datetime.now(UTC),
    )


def _chat() -> Chat:
    return Chat(
        id=uuid4(),
        telegram_chat_id=-1001234567890,
        chat_name="Acme",
        status="active",
        created_at=datetime.now(UTC),
    )


def _failed(risk_event_id: Any) -> FailedAlert:
    return FailedAlert(
        id=uuid4(),
        risk_event_id=risk_event_id,
        channel="#alerts",
        retry_count=0,
        resolved=False,
        created_at=datetime.now(UTC),
    )


def _patch_common(mp: Any, *, event: RiskEvent | None) -> dict[str, AsyncMock]:
    mp.setattr(workers_mod, "acquire_connection", lambda: _NullAcquire())
    mocks = {
        "get_by_ref": AsyncMock(return_value=event),
        "get_chat_by_id": AsyncMock(return_value=_chat()),
        "get_partner_by_id": AsyncMock(return_value=None),
        "get_message_timestamp": AsyncMock(return_value=None),
        "set_slack_message_ts": AsyncMock(),
        "mark_failed_alert_resolved": AsyncMock(),
        "bump_failed_alert_attempt": AsyncMock(),
    }
    for name, m in mocks.items():
        mp.setattr(workers_mod, name, m)
    return mocks


async def test_retry_delivers_and_resolves(monkeypatch: Any) -> None:
    event = _event(slack_ts=None)
    mocks = _patch_common(monkeypatch, event=event)
    monkeypatch.setattr(workers_mod, "post_alert", AsyncMock(return_value="1700.1"))

    await workers_mod._retry_one_failed_alert(MagicMock(), _failed(event.id))

    mocks["set_slack_message_ts"].assert_awaited_once()
    mocks["mark_failed_alert_resolved"].assert_awaited_once()
    mocks["bump_failed_alert_attempt"].assert_not_awaited()


async def test_retry_still_failing_bumps_attempt(monkeypatch: Any) -> None:
    event = _event(slack_ts=None)
    mocks = _patch_common(monkeypatch, event=event)
    monkeypatch.setattr(
        workers_mod, "post_alert", AsyncMock(side_effect=SlackDeliveryError("down"))
    )

    await workers_mod._retry_one_failed_alert(MagicMock(), _failed(event.id))

    mocks["bump_failed_alert_attempt"].assert_awaited_once()
    mocks["mark_failed_alert_resolved"].assert_not_awaited()
    mocks["set_slack_message_ts"].assert_not_awaited()


async def test_retry_orphan_event_resolves_without_posting(monkeypatch: Any) -> None:
    mocks = _patch_common(monkeypatch, event=None)  # event gone
    post = AsyncMock()
    monkeypatch.setattr(workers_mod, "post_alert", post)

    await workers_mod._retry_one_failed_alert(MagicMock(), _failed(uuid4()))

    post.assert_not_awaited()
    mocks["mark_failed_alert_resolved"].assert_awaited_once()


async def test_retry_already_delivered_is_skipped(monkeypatch: Any) -> None:
    event = _event(slack_ts="1699.9")  # already has a ts → delivered elsewhere
    mocks = _patch_common(monkeypatch, event=event)
    post = AsyncMock()
    monkeypatch.setattr(workers_mod, "post_alert", post)

    await workers_mod._retry_one_failed_alert(MagicMock(), _failed(event.id))

    post.assert_not_awaited()
    mocks["mark_failed_alert_resolved"].assert_awaited_once()
