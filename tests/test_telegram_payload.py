"""Regression: incoming-payload serialization must tolerate aiogram Default.

A message whose sender toggled the link preview arrives with a partial
``link_preview_options`` — its unset fields hold ``Default('...')`` sentinels, not
None. A plain ``model_dump(mode="json")`` raises PydanticSerializationError, which
used to drop the whole update at webhook dispatch (``webhook.dispatch_failed``).
``dump_incoming`` must serialize it cleanly.
"""

from __future__ import annotations

from aiogram.client.default import Default
from aiogram.types import BusinessMessagesDeleted, Message

from src.utils.telegram import dump_incoming

_BASE = {
    "message_id": 5,
    "date": 1700000000,
    "chat": {"id": 1, "type": "group", "title": "t"},
    "from": {"id": 2, "is_bot": False, "first_name": "A"},
    "text": "check https://example.com",
}


def test_message_with_partial_link_preview_serializes() -> None:
    msg = Message.model_validate({**_BASE, "link_preview_options": {"is_disabled": True}})
    # The offending sentinels are really present on the validated object.
    assert isinstance(msg.link_preview_options.prefer_small_media, Default)

    out = dump_incoming(msg)  # must not raise

    assert out["message_id"] == 5
    lpo = out["link_preview_options"]
    assert lpo["is_disabled"] is True
    # Default → null (not the raw sentinel).
    assert lpo["prefer_small_media"] is None
    assert lpo["show_above_text"] is None


def test_plain_message_matches_vanilla_dump() -> None:
    """No Default present → identical to a plain model_dump (safe drop-in)."""
    msg = Message.model_validate(_BASE)
    assert dump_incoming(msg) == msg.model_dump(mode="json", exclude_none=True)


def test_dump_incoming_is_json_serializable() -> None:
    """Result must round-trip through json.dumps (it is stored as JSONB)."""
    import json

    msg = Message.model_validate({**_BASE, "link_preview_options": {"is_disabled": True}})
    json.dumps(dump_incoming(msg))  # must not raise


def test_exclude_none_false_keeps_nulls() -> None:
    deleted = BusinessMessagesDeleted.model_validate(
        {
            "business_connection_id": "abc",
            "chat": {"id": 9, "type": "private", "first_name": "P"},
            "message_ids": [1, 2, 3],
        }
    )
    out = dump_incoming(deleted, exclude_none=False)
    assert out["message_ids"] == [1, 2, 3]
