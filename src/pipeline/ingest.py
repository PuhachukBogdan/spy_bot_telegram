"""Message ingestion. Phase 5.

Turns an incoming aiogram ``Message`` (already gated to an active partner chat by
the whitelist middleware) into a row in the ``messages`` table: classifies the
content type and sender role, extracts links/mentions, resolves forwards,
detects language, and stores the full Telegram payload. Idempotent on
``(chat_id, telegram_message_id)``.

Tier-1 rule matching (CLAUDE.md 7.1 steps 6-9) hooks in here in Phase 6; for now
``has_triggers`` / ``base_score`` keep their column defaults.
"""

from __future__ import annotations

from uuid import UUID

import asyncpg
from aiogram.enums import MessageEntityType
from aiogram.types import (
    Message,
    MessageEntity,
    MessageOriginChannel,
    MessageOriginChat,
    MessageOriginUser,
)

from src.config import settings
from src.db.client import acquire_connection
from src.db.models import Chat
from src.db.queries.etc import find_internal_user_by_telegram_id
from src.db.queries.messages import insert_message, update_message_triggers
from src.db.queries.queue import enqueue_task
from src.pipeline.tier1 import MatchResult, pattern_cache
from src.utils.language import detect_language
from src.utils.logging import get_logger

log = get_logger(__name__)

# Below this many stripped chars (and not a voice note), a message is unlikely to
# matter to a weekly summary; precomputed here so the summary filter is cheap.
_SIGNIFICANT_MIN_CHARS = 3


async def ingest_message(message: Message, chat: Chat) -> None:
    """Persist one message from an active partner chat (CLAUDE.md 7.1 step 5)."""
    text = message.text if message.text is not None else message.caption
    message_type = _classify_type(message)
    forward_from_id, forward_from_chat_id = _resolve_forward(message)
    links = _extract_links(message)
    mentions = _extract_mentions(message)
    is_significant = _compute_significance(text, message_type)

    async with acquire_connection() as conn:
        sender_role = await _resolve_sender_role(conn, message)
        stored = await insert_message(
            conn,
            telegram_message_id=message.message_id,
            chat_id=chat.id,
            sender_id=message.from_user.id if message.from_user else None,
            sender_chat_id=message.sender_chat.id if message.sender_chat else None,
            sender_name=_sender_name(message),
            sender_role=sender_role,
            message_text=text,
            message_type=message_type,
            timestamp=message.date,
            reply_to_message_id=(
                message.reply_to_message.message_id
                if message.reply_to_message is not None
                else None
            ),
            forward_from_id=forward_from_id,
            forward_from_chat_id=forward_from_chat_id,
            message_thread_id=message.message_thread_id,
            links=links or None,
            mentions=mentions or None,
            detected_language=detect_language(text),
            is_significant=is_significant,
            raw_payload=message.model_dump(mode="json", exclude_none=True),
        )

        if stored is None:
            log.debug(
                "ingest.duplicate",
                chat_id=chat.telegram_chat_id,
                msg_id=message.message_id,
            )
            return

        # Tier-1 rule matching (CLAUDE.md 7.1 steps 6-9): cheap, in-memory.
        result = pattern_cache.match(text, sender_role)
        await update_message_triggers(
            conn,
            stored.id,
            has_triggers=result.has_triggers,
            base_score=result.base_score,
            triggered_patterns=result.triggered_patterns,
        )
        await _enqueue_followups(conn, stored.id, message_type, result)

    log.info(
        "ingest.stored",
        chat_id=chat.telegram_chat_id,
        thread_id=chat.message_thread_id,
        msg_id=message.message_id,
        type=message_type,
        role=sender_role,
        significant=is_significant,
        forwarded=forward_from_id is not None or forward_from_chat_id is not None,
        forward_from_id=forward_from_id,
        has_triggers=result.has_triggers,
        base_score=result.base_score,
    )


async def _enqueue_followups(
    conn: asyncpg.Connection,
    message_id: UUID,
    message_type: str,
    result: MatchResult,
) -> None:
    """Queue heavy follow-up work (CLAUDE.md 7.1 steps 8-9).

    Voice / video notes go to the Whisper queue (transcription runs Tier-1 again
    in Phase 7). Otherwise a base_score at/above the threshold gets a real-time
    priority LLM pass (Phase 10).
    """
    if message_type in ("voice", "video_note"):
        await enqueue_task(conn, "whisper_transcribe", {"message_id": str(message_id)})
    elif result.base_score >= settings.PRIORITY_SCORE_THRESHOLD:
        await enqueue_task(conn, "priority_llm", {"message_id": str(message_id)})


def _classify_type(message: Message) -> str:
    """Map a message to a coarse content type stored in ``messages.message_type``.

    Order matters: voice/video_note first (they need transcription), then specific
    media before the generic ``document`` (animations also expose ``.document``).
    """
    if message.text is not None:
        return "text"
    if message.voice is not None:
        return "voice"
    if message.video_note is not None:
        return "video_note"
    if message.photo:
        return "photo"
    if message.video is not None:
        return "video"
    if message.audio is not None:
        return "audio"
    if message.animation is not None:
        return "animation"
    if message.document is not None:
        return "document"
    if message.sticker is not None:
        return "sticker"
    if message.poll is not None:
        return "poll"
    if message.location is not None:
        return "location"
    if message.contact is not None:
        return "contact"
    if message.caption is not None:
        return "media"
    return "other"


def _sender_name(message: Message) -> str | None:
    """Human-readable sender label: user full name / @username / chat title."""
    if message.from_user is not None:
        user = message.from_user
        if user.full_name:
            return user.full_name
        return f"@{user.username}" if user.username else None
    if message.sender_chat is not None:
        sender_chat = message.sender_chat
        if sender_chat.title:
            return sender_chat.title
        return f"@{sender_chat.username}" if sender_chat.username else None
    return None


async def _resolve_sender_role(conn: asyncpg.Connection, message: Message) -> str:
    """Classify the sender as internal / partner / anonymous_admin / unknown.

    A real user is ``internal`` if their Telegram id maps to an enabled
    ``internal_users`` row, else ``partner``; other bots are ``unknown``. A
    ``sender_chat`` equal to the chat itself is an ``anonymous_admin`` posting on
    the group's behalf (CLAUDE.md 11.8); any other ``sender_chat`` (e.g. a linked
    channel) is ``unknown``.
    """
    if message.from_user is not None:
        if message.from_user.is_bot:
            return "unknown"
        internal = await find_internal_user_by_telegram_id(conn, message.from_user.id)
        return "internal" if internal is not None else "partner"
    if message.sender_chat is not None:
        if message.sender_chat.id == message.chat.id:
            return "anonymous_admin"
        return "unknown"
    return "unknown"


def _resolve_forward(message: Message) -> tuple[int | None, int | None]:
    """Return ``(forward_from_id, forward_from_chat_id)`` from the forward origin."""
    origin = message.forward_origin
    if origin is None:
        return None, None
    if isinstance(origin, MessageOriginUser):
        return origin.sender_user.id, None
    if isinstance(origin, MessageOriginChannel):
        return None, origin.chat.id
    if isinstance(origin, MessageOriginChat):
        return None, origin.sender_chat.id
    # MessageOriginHiddenUser carries only a display name; nothing to key on.
    return None, None


def _extract_links(message: Message) -> list[str]:
    """Collect URLs from message entities (``url`` text + ``text_link`` targets)."""
    text, entities = _text_and_entities(message)
    links: list[str] = []
    for ent in entities:
        if ent.type == MessageEntityType.TEXT_LINK and ent.url:
            links.append(ent.url)
        elif ent.type == MessageEntityType.URL and text is not None:
            links.append(ent.extract_from(text))
    return list(dict.fromkeys(links))  # dedupe, preserve order


def _extract_mentions(message: Message) -> list[str]:
    """Collect @username mentions and named text-mentions from entities."""
    text, entities = _text_and_entities(message)
    mentions: list[str] = []
    for ent in entities:
        if ent.type == MessageEntityType.MENTION and text is not None:
            mentions.append(ent.extract_from(text))
        elif ent.type == MessageEntityType.TEXT_MENTION and ent.user is not None:
            mentions.append(ent.user.full_name or f"id:{ent.user.id}")
    return list(dict.fromkeys(mentions))


def _text_and_entities(message: Message) -> tuple[str | None, list[MessageEntity]]:
    """Return the active text and its entities (message body or media caption)."""
    if message.text is not None:
        return message.text, list(message.entities or [])
    return message.caption, list(message.caption_entities or [])


def _compute_significance(message_text: str | None, message_type: str) -> bool:
    """Precompute ``is_significant`` for the summary noise filter (heuristic)."""
    if message_type in ("voice", "video_note"):
        return True  # will carry text once transcribed (Phase 7)
    return bool(message_text and len(message_text.strip()) >= _SIGNIFICANT_MIN_CHARS)
