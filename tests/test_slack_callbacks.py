"""Tests for Phase 12: Slack interactive button callbacks."""

from __future__ import annotations

import hashlib
import hmac
import json
import time
import urllib.parse
from unittest.mock import AsyncMock, MagicMock
from uuid import uuid4

import pytest
from starlette.datastructures import Headers

from src.alerts.slack import build_alert_blocks
from src.alerts.slack_callbacks import (
    _ACTION_TO_STATUS,
    _channel_for_level,
    _parse_payload,
    handle_slack_action,
    verify_slack_signature,
)

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _make_headers(
    raw_body: bytes,
    secret: str = "test_secret",
    ts: int | None = None,
) -> Headers:
    ts = ts or int(time.time())
    base = f"v0:{ts}:{raw_body.decode()}"
    sig = "v0=" + hmac.new(
        key=secret.encode(),
        msg=base.encode(),
        digestmod=hashlib.sha256,
    ).hexdigest()
    return Headers(
        {
            "x-slack-request-timestamp": str(ts),
            "x-slack-signature": sig,
        }
    )


def _make_payload(
    action_id: str = "mark_confirmed",
    value: str | None = None,
    msg_ts: str = "1700000000.000100",
    user_id: str = "UTEST",
    user_name: str = "tester",
) -> bytes:
    value = value or str(uuid4())
    payload = {
        "type": "block_actions",
        "user": {"id": user_id, "name": user_name},
        "message": {"ts": msg_ts},
        "actions": [{"action_id": action_id, "value": value, "type": "button"}],
    }
    encoded = urllib.parse.urlencode({"payload": json.dumps(payload)})
    return encoded.encode()


# ---------------------------------------------------------------------------
# verify_slack_signature
# ---------------------------------------------------------------------------


def test_valid_signature_passes(monkeypatch: pytest.MonkeyPatch) -> None:
    raw = b"payload=%7B%22type%22%3A%22block_actions%22%7D"
    secret = "slack_test_secret_123"
    monkeypatch.setattr(
        "src.alerts.slack_callbacks.settings.SLACK_SIGNING_SECRET",
        MagicMock(get_secret_value=lambda: secret),
    )
    headers = _make_headers(raw, secret=secret)
    assert verify_slack_signature(headers, raw) is True


def test_wrong_signature_rejected(monkeypatch: pytest.MonkeyPatch) -> None:
    raw = b"payload=%7B%7D"
    monkeypatch.setattr(
        "src.alerts.slack_callbacks.settings.SLACK_SIGNING_SECRET",
        MagicMock(get_secret_value=lambda: "correct_secret"),
    )
    headers = _make_headers(raw, secret="wrong_secret")
    assert verify_slack_signature(headers, raw) is False


def test_expired_timestamp_rejected(monkeypatch: pytest.MonkeyPatch) -> None:
    raw = b"payload=%7B%7D"
    secret = "s"
    monkeypatch.setattr(
        "src.alerts.slack_callbacks.settings.SLACK_SIGNING_SECRET",
        MagicMock(get_secret_value=lambda: secret),
    )
    old_ts = int(time.time()) - 400  # beyond 5-min window
    headers = _make_headers(raw, secret=secret, ts=old_ts)
    assert verify_slack_signature(headers, raw) is False


def test_non_numeric_timestamp_rejected(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        "src.alerts.slack_callbacks.settings.SLACK_SIGNING_SECRET",
        MagicMock(get_secret_value=lambda: "s"),
    )
    headers = Headers(
        {"x-slack-request-timestamp": "not-a-number", "x-slack-signature": "v0=abc"}
    )
    assert verify_slack_signature(headers, b"body") is False


# ---------------------------------------------------------------------------
# _parse_payload
# ---------------------------------------------------------------------------


def test_parse_payload_roundtrip() -> None:
    data = {"type": "block_actions", "actions": [{"action_id": "mark_fp"}]}
    encoded = urllib.parse.urlencode({"payload": json.dumps(data)}).encode()
    result = _parse_payload(encoded)
    assert result["type"] == "block_actions"
    assert result["actions"][0]["action_id"] == "mark_fp"


def test_parse_payload_missing_key_raises() -> None:
    with pytest.raises(KeyError):
        _parse_payload(b"no_payload_key=foo")


# ---------------------------------------------------------------------------
# handle_slack_action — dispatch
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_handle_unknown_type_is_noop() -> None:
    payload = json.dumps({"type": "view_submission"})
    raw = urllib.parse.urlencode({"payload": payload}).encode()
    # Should return without calling any DB function (no monkeypatching needed)
    await handle_slack_action(raw)


@pytest.mark.asyncio
async def test_handle_empty_actions_is_noop() -> None:
    payload = json.dumps({"type": "block_actions", "actions": []})
    raw = urllib.parse.urlencode({"payload": payload}).encode()
    await handle_slack_action(raw)


@pytest.mark.asyncio
async def test_handle_unknown_action_id_is_noop() -> None:
    payload = json.dumps(
        {
            "type": "block_actions",
            "user": {"id": "U1", "name": "alice"},
            "message": {"ts": "1700000000.000100"},
            "actions": [{"action_id": "some_other_action", "value": str(uuid4())}],
        }
    )
    raw = urllib.parse.urlencode({"payload": payload}).encode()
    # No error raised, no DB calls
    await handle_slack_action(raw)


@pytest.mark.asyncio
async def test_handle_bad_uuid_value_is_noop() -> None:
    payload = json.dumps(
        {
            "type": "block_actions",
            "user": {"id": "U1", "name": "alice"},
            "message": {"ts": "1700000000.000100"},
            "actions": [{"action_id": "mark_confirmed", "value": "not-a-uuid"}],
        }
    )
    raw = urllib.parse.urlencode({"payload": payload}).encode()
    await handle_slack_action(raw)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "action_id,expected_status",
    [
        ("mark_confirmed", "confirmed"),
        ("mark_fp", "false_positive"),
        ("mark_escalated", "escalated"),
    ],
)
async def test_handle_valid_action_calls_do_mark(
    monkeypatch: pytest.MonkeyPatch, action_id: str, expected_status: str
) -> None:
    event_id = uuid4()
    fake_event = MagicMock()
    fake_event.risk_level = "high"
    fake_event.slack_message_ts = "1700000000.000100"

    do_mark_mock = AsyncMock(return_value=fake_event)
    update_msg_mock = AsyncMock()
    monkeypatch.setattr("src.alerts.slack_callbacks._do_mark", do_mark_mock)
    monkeypatch.setattr(
        "src.alerts.slack_callbacks._update_slack_message", update_msg_mock
    )

    raw = _make_payload(action_id=action_id, value=str(event_id), msg_ts="1700000000.000100")
    await handle_slack_action(raw)

    do_mark_mock.assert_awaited_once()
    call_kwargs = do_mark_mock.call_args
    assert call_kwargs.args[0] == event_id
    assert call_kwargs.args[1] == expected_status
    update_msg_mock.assert_awaited_once()


@pytest.mark.asyncio
async def test_handle_event_not_found_skips_message_update(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    do_mark_mock = AsyncMock(return_value=None)
    update_msg_mock = AsyncMock()
    monkeypatch.setattr("src.alerts.slack_callbacks._do_mark", do_mark_mock)
    monkeypatch.setattr(
        "src.alerts.slack_callbacks._update_slack_message", update_msg_mock
    )

    raw = _make_payload(action_id="mark_confirmed")
    await handle_slack_action(raw)

    do_mark_mock.assert_awaited_once()
    update_msg_mock.assert_not_awaited()


@pytest.mark.asyncio
async def test_handle_malformed_body_is_noop() -> None:
    raw = b"this_is_not_url_encoded_json"
    # Should log and return without raising
    await handle_slack_action(raw)


# ---------------------------------------------------------------------------
# _channel_for_level
# ---------------------------------------------------------------------------


def test_channel_for_critical(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        "src.alerts.slack_callbacks.settings",
        MagicMock(SLACK_CHANNEL_ALERTS="C_ALERTS"),
    )
    assert _channel_for_level("critical") == "C_ALERTS"


def test_channel_for_high(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        "src.alerts.slack_callbacks.settings",
        MagicMock(SLACK_CHANNEL_ALERTS="C_ALERTS"),
    )
    assert _channel_for_level("high") == "C_ALERTS"


# ---------------------------------------------------------------------------
# build_alert_blocks — action button inclusion
# ---------------------------------------------------------------------------


def _make_risk_event(**kwargs: object) -> MagicMock:
    ev = MagicMock()
    ev.id = uuid4()
    ev.risk_level = kwargs.get("risk_level", "high")
    ev.final_score = kwargs.get("final_score", 65)
    ev.risk_type = kwargs.get("risk_type", "shadow_deal")
    ev.llm_verdict = "confirmed"
    ev.llm_explanation = "test explanation"
    ev.detected_phrase = "test phrase"
    return ev


def _make_chat() -> MagicMock:
    chat = MagicMock()
    chat.chat_name = "Test Chat"
    chat.telegram_chat_id = -100123456789
    return chat


def test_build_alert_blocks_includes_actions_by_default() -> None:
    event = _make_risk_event()
    chat = _make_chat()
    blocks, _ = build_alert_blocks(event, chat, "Partner A")
    block_types = [b["type"] for b in blocks]
    assert "actions" in block_types


def test_build_alert_blocks_no_actions_when_disabled() -> None:
    event = _make_risk_event()
    chat = _make_chat()
    blocks, _ = build_alert_blocks(event, chat, "Partner A", include_actions=False)
    block_types = [b["type"] for b in blocks]
    assert "actions" not in block_types


def test_build_alert_blocks_action_ids_correct() -> None:
    event = _make_risk_event()
    chat = _make_chat()
    blocks, _ = build_alert_blocks(event, chat, "Partner A")
    actions_block = next(b for b in blocks if b["type"] == "actions")
    action_ids = {el["action_id"] for el in actions_block["elements"]}
    assert action_ids == {"mark_confirmed", "mark_fp", "mark_escalated"}


def test_build_alert_blocks_action_values_are_event_uuid() -> None:
    event = _make_risk_event()
    chat = _make_chat()
    blocks, _ = build_alert_blocks(event, chat, "Partner A")
    actions_block = next(b for b in blocks if b["type"] == "actions")
    for el in actions_block["elements"]:
        assert el["value"] == str(event.id)


# ---------------------------------------------------------------------------
# _ACTION_TO_STATUS coverage
# ---------------------------------------------------------------------------


def test_action_to_status_mapping_complete() -> None:
    assert set(_ACTION_TO_STATUS.keys()) == {"mark_fp", "mark_confirmed", "mark_escalated"}
    assert _ACTION_TO_STATUS["mark_fp"] == "false_positive"
    assert _ACTION_TO_STATUS["mark_confirmed"] == "confirmed"
    assert _ACTION_TO_STATUS["mark_escalated"] == "escalated"
