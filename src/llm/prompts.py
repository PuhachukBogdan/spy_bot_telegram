"""Prompt resolution + injection-safe conversation rendering. Phase 8.

Two jobs:
  * :func:`load_template` resolves a system-prompt template — the active DB row in
    ``prompts`` if its template is non-empty, else the ``prompts/<name>.txt`` file
    fallback (the seed ships empty DB templates on purpose, pipeline §7.6).
  * :func:`build_conversation_block` renders messages into the
    ``<conversation>…</conversation>`` user payload. Every piece of message-
    derived text is HTML-escaped, so a partner cannot inject a closing tag or a
    fake instruction that breaks out of the data envelope (prompt-injection
    defense — the system prompt tells the model that everything inside the tags is
    untrusted data).
"""

from __future__ import annotations

from collections.abc import Iterable
from html import escape
from pathlib import Path
from uuid import UUID

import asyncpg

from src.db.models import Message
from src.db.queries.prompts import get_active_prompt
from src.utils.logging import get_logger

log = get_logger(__name__)

# Repo-root prompts/ dir: src/llm/prompts.py -> parents[2] is the project root.
PROMPTS_DIR = Path(__file__).resolve().parents[2] / "prompts"


class PromptNotFoundError(RuntimeError):
    """Neither an active DB template nor a non-empty file fallback was found."""


async def load_template(conn: asyncpg.Connection, name: str) -> str:
    """Resolve a system-prompt template: active DB row, else ``prompts/<name>.txt``.

    Raises :class:`PromptNotFoundError` if both are empty/missing — a hard config
    error worth failing on rather than calling the LLM with no instructions.
    """
    row = await get_active_prompt(conn, name)
    if row is not None and row.template.strip():
        return row.template

    path = PROMPTS_DIR / f"{name}.txt"
    if path.is_file():
        text = path.read_text(encoding="utf-8").strip()
        if text:
            return text

    raise PromptNotFoundError(
        f"no active DB template and no non-empty {path.name} fallback for {name!r}"
    )


def build_conversation_block(
    messages: Iterable[Message], flagged_ids: Iterable[UUID]
) -> str:
    """Render messages into the injection-safe ``<conversation>`` user payload.

    Each message becomes ``<message id=… sender_role=… flagged=…>text</message>``
    with all dynamic values escaped. Voice/video notes use their transcription as
    the text. A trailing ``<flagged_messages>[…]</flagged_messages>`` lists the
    ids the model must analyse.
    """
    flagged = {str(fid) for fid in flagged_ids}
    lines = ["<conversation>"]
    for m in messages:
        mid = str(m.id)
        text = m.message_text or m.transcription or ""
        lines.append(
            f'  <message id="{escape(mid, quote=True)}" '
            f'sender_role="{escape(m.sender_role, quote=True)}" '
            f'flagged="{"true" if mid in flagged else "false"}">'
            f"{escape(text)}</message>"
        )
    lines.append("</conversation>")
    lines.append(f"<flagged_messages>[{', '.join(sorted(flagged))}]</flagged_messages>")
    return "\n".join(lines)
