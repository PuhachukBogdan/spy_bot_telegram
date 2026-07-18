"""JSON-safe serialization for *incoming* aiogram objects.

aiogram fills optional sub-model fields that Telegram omitted with a ``Default``
sentinel rather than ``None`` — e.g. a message whose sender toggled the link
preview arrives with ``link_preview_options`` where only ``is_disabled`` is set
and ``prefer_small_media`` / ``prefer_large_media`` / ``show_above_text`` hold
``Default('...')``. Those sentinels are not JSON-serializable, so a plain
``model_dump(mode="json")`` raises ``PydanticSerializationError`` and — since our
webhook stores the raw payload during ingest — the entire update is dropped
(never stored, never analysed).

``dump_incoming`` routes every raw-payload dump through a fallback that maps the
sentinel (meaning "not specified") to ``null``, so such messages ingest normally.
"""

from __future__ import annotations

from typing import Any

from aiogram.client.default import Default
from pydantic import BaseModel


def _json_fallback(value: Any) -> Any:
    """Last-resort serializer for values pydantic can't encode to JSON.

    On an incoming aiogram object the only such value is the ``Default`` sentinel
    ("field not specified by Telegram") → ``None``. Anything else unexpected also
    collapses to ``None`` so storing a forensic payload can never crash ingest.
    """
    if isinstance(value, Default):
        return None
    return None


def dump_incoming(obj: BaseModel, *, exclude_none: bool = True) -> dict[str, Any]:
    """``model_dump(mode="json")`` that tolerates aiogram ``Default`` sentinels.

    Identical to a plain ``model_dump(mode="json", exclude_none=...)`` when no
    sentinel is present (the fallback only fires for otherwise-unserializable
    values), so it is a safe drop-in for any incoming-payload dump.
    """
    return obj.model_dump(
        mode="json", exclude_none=exclude_none, fallback=_json_fallback
    )
