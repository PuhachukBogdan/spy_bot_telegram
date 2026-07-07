"""messages / message_edits queries. Phase 5+.

Plain-SQL helpers over the ``messages`` and ``message_edits`` tables. Each takes
an already-acquired ``asyncpg.Connection`` so the caller controls the
pool/transaction boundary.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any
from uuid import UUID

import asyncpg

from src.db.models import Message


async def insert_message(
    conn: asyncpg.Connection,
    *,
    telegram_message_id: int,
    chat_id: UUID,
    sender_id: int | None,
    sender_chat_id: int | None,
    sender_name: str | None,
    sender_role: str,
    message_text: str | None,
    message_type: str,
    timestamp: datetime,
    reply_to_message_id: int | None = None,
    forward_from_id: int | None = None,
    forward_from_chat_id: int | None = None,
    message_thread_id: int | None = None,
    links: list[str] | None = None,
    mentions: list[str] | None = None,
    detected_language: str | None = None,
    is_significant: bool = False,
    source: str = "live_group",
    business_connection_id: str | None = None,
    business_peer_user_id: int | None = None,
    raw_payload: dict[str, Any] | None = None,
) -> Message | None:
    """Insert one ingested message; return it, or ``None`` if it already existed.

    Idempotent via ``ON CONFLICT (chat_id, telegram_message_id) DO NOTHING``
    (CLAUDE.md 7.1 step 5 / 11.1): Telegram retries the same webhook for up to
    24h, so a duplicate insert is a no-op and returns ``None``. Tier-1 fields
    (``has_triggers`` / ``base_score`` / ``triggered_patterns``) keep their column
    defaults here and are filled by the Phase-6 matcher.

    ``source`` records how the message reached us (``live_group`` / ``live_topic``
    / ``business`` / ``imported``); the business columns are populated only for
    Telegram Business messages (migration 0006), ``NULL`` otherwise.
    """
    row = await conn.fetchrow(
        """
        INSERT INTO messages (
            telegram_message_id, chat_id, sender_id, sender_chat_id, sender_name,
            sender_role, message_text, message_type, timestamp,
            reply_to_message_id, forward_from_id, forward_from_chat_id,
            message_thread_id, links, mentions, detected_language,
            is_significant, source, business_connection_id, business_peer_user_id,
            raw_payload
        )
        VALUES (
            $1, $2, $3, $4, $5,
            $6, $7, $8, $9,
            $10, $11, $12,
            $13, $14, $15, $16,
            $17, $18, $19, $20,
            $21
        )
        ON CONFLICT (chat_id, telegram_message_id) DO NOTHING
        RETURNING *
        """,
        telegram_message_id,
        chat_id,
        sender_id,
        sender_chat_id,
        sender_name,
        sender_role,
        message_text,
        message_type,
        timestamp,
        reply_to_message_id,
        forward_from_id,
        forward_from_chat_id,
        message_thread_id,
        links,
        mentions,
        detected_language,
        is_significant,
        source,
        business_connection_id,
        business_peer_user_id,
        raw_payload,
    )
    return Message.from_record(row) if row is not None else None


async def get_message(
    conn: asyncpg.Connection, chat_id: UUID, telegram_message_id: int
) -> Message | None:
    """Return a stored message by its (chat, telegram id) key, or ``None``.

    Served by the UNIQUE index on ``(chat_id, telegram_message_id)``. Used by the
    edit handler to find the original row before recording an edit.
    """
    row = await conn.fetchrow(
        """
        SELECT * FROM messages
        WHERE chat_id = $1 AND telegram_message_id = $2
        """,
        chat_id,
        telegram_message_id,
    )
    return Message.from_record(row) if row is not None else None


async def get_message_by_id(
    conn: asyncpg.Connection, message_id: UUID
) -> Message | None:
    """Return a message by its primary-key UUID, or ``None``.

    Used by queue workers (Phase 7+), whose task payloads carry the message's
    internal id rather than the ``(chat, telegram id)`` pair.
    """
    row = await conn.fetchrow("SELECT * FROM messages WHERE id = $1", message_id)
    return Message.from_record(row) if row is not None else None


async def get_message_timestamp(
    conn: asyncpg.Connection, message_id: UUID
) -> datetime | None:
    """Return a message's Telegram send time (``timestamp``) by id, or ``None``.

    Used by alert dispatch to show WHEN the flagged message was sent, not when it
    was analysed: ``created_at`` is the ingestion / analysis cursor, ``timestamp``
    is the real Telegram send time. A targeted single-column read for the hot
    dispatch path (no need to hydrate the full row).
    """
    ts = await conn.fetchval("SELECT timestamp FROM messages WHERE id = $1", message_id)
    return ts if isinstance(ts, datetime) else None


async def file_hash_seen(
    conn: asyncpg.Connection, chat_id: UUID, content_hash: str
) -> bool:
    """True if a document with this exact content was already analysed in this chat."""
    row = await conn.fetchrow(
        "SELECT 1 FROM analyzed_file_hashes WHERE chat_id = $1 AND content_hash = $2",
        chat_id,
        content_hash,
    )
    return row is not None


async def record_file_hash(
    conn: asyncpg.Connection, *, chat_id: UUID, content_hash: str, message_id: UUID
) -> None:
    """Mark a (chat, content) pair as analysed. Idempotent (dupe insert is a no-op)."""
    await conn.execute(
        """
        INSERT INTO analyzed_file_hashes (chat_id, content_hash, message_id)
        VALUES ($1, $2, $3)
        ON CONFLICT (chat_id, content_hash) DO NOTHING
        """,
        chat_id,
        content_hash,
        message_id,
    )


async def get_messages_by_ids(
    conn: asyncpg.Connection, message_ids: list[UUID]
) -> list[Message]:
    """Return messages for the given ids, oldest first (chronological context).

    Used to render the surrounding lines of a risk case on the alert card. Order
    is by ``timestamp`` so the snippet reads in the order it was said; ids with no
    row are simply absent.
    """
    if not message_ids:
        return []
    rows = await conn.fetch(
        "SELECT * FROM messages WHERE id = ANY($1) ORDER BY timestamp ASC",
        message_ids,
    )
    return [Message.from_record(row) for row in rows]


async def get_messages_around(
    conn: asyncpg.Connection,
    chat_id: UUID,
    anchor_ts: datetime,
    *,
    before: int = 3,
    after: int = 3,
) -> tuple[list[Message], list[Message]]:
    """Return ``(before, after)`` context messages around an anchor timestamp.

    Used by ``/risk`` to show the conversation around a flagged message: up to
    ``before`` messages strictly older and ``after`` strictly newer, both in the
    same chat unit. Each list is returned in chronological order (oldest first).
    The anchor itself is excluded (the caller already has it).
    """
    before_rows = await conn.fetch(
        """
        SELECT * FROM messages
        WHERE chat_id = $1 AND timestamp < $2
        ORDER BY timestamp DESC
        LIMIT $3
        """,
        chat_id,
        anchor_ts,
        before,
    )
    after_rows = await conn.fetch(
        """
        SELECT * FROM messages
        WHERE chat_id = $1 AND timestamp > $2
        ORDER BY timestamp ASC
        LIMIT $3
        """,
        chat_id,
        anchor_ts,
        after,
    )
    before_list = [Message.from_record(row) for row in reversed(before_rows)]
    after_list = [Message.from_record(row) for row in after_rows]
    return before_list, after_list


async def get_chat_analysis_window(
    conn: asyncpg.Connection,
    chat_id: UUID,
    *,
    since: datetime | None,
    limit: int,
    context_before: int,
) -> tuple[list[Message], list[Message]]:
    """Return ``(context, new)`` for one Tier-2 analysis pass (Phase 9).

    The cursor is ``created_at`` (ingestion time: microsecond, monotonic, and
    bumpable on edit via :func:`bump_message_for_analysis`), NOT the Telegram
    ``timestamp`` — see migration 0010 for why. ``new`` is the messages with
    ``created_at > since`` (or all, when ``since`` is NULL — first run), oldest
    first, capped at ``limit``; ``context`` is up to ``context_before`` messages
    strictly older than the first new one, for the LLM to read the lead-up.
    Soft-deleted messages (``deleted_at`` set) are excluded. Returns ``([], [])``
    when there is nothing new.
    """
    new_rows = await conn.fetch(
        """
        SELECT * FROM messages
        WHERE chat_id = $1
          AND ($2::timestamptz IS NULL OR created_at > $2)
          AND deleted_at IS NULL
        ORDER BY created_at ASC
        LIMIT $3
        """,
        chat_id,
        since,
        limit,
    )
    new = [Message.from_record(row) for row in new_rows]
    if not new:
        return [], []

    ctx_rows = await conn.fetch(
        """
        SELECT * FROM messages
        WHERE chat_id = $1 AND created_at < $2 AND deleted_at IS NULL
        ORDER BY created_at DESC
        LIMIT $3
        """,
        chat_id,
        new[0].created_at,
        context_before,
    )
    context = [Message.from_record(row) for row in reversed(ctx_rows)]
    return context, new


async def bump_message_for_analysis(
    conn: asyncpg.Connection, message_id: UUID
) -> None:
    """Move a message to the head of its chat's Tier-2 analysis window (Phase 9.1).

    The window cursor is ``created_at`` (see :func:`get_chat_analysis_window`).
    Setting it to ``now()`` makes an already-analysed message re-enter the next
    pass as the newest item — used when an edit introduces a risk phrase after the
    watermark already passed the message, so the edited text gets a fresh look with
    current context. Only ``created_at`` moves; the Telegram ``timestamp`` (true
    send-time, used by the review surface) is untouched.
    """
    await conn.execute(
        "UPDATE messages SET created_at = now() WHERE id = $1", message_id
    )


async def update_message_transcription(
    conn: asyncpg.Connection, message_id: UUID, transcription: str | None
) -> None:
    """Store the Whisper transcript on a voice / video_note message (Phase 7)."""
    await conn.execute(
        "UPDATE messages SET transcription = $2 WHERE id = $1",
        message_id,
        transcription,
    )


async def insert_message_edit(
    conn: asyncpg.Connection,
    *,
    message_id: UUID,
    old_text: str | None,
    new_text: str | None,
    edited_at: datetime,
) -> None:
    """Append an edit-history row for a message (CLAUDE.md 7.3)."""
    await conn.execute(
        """
        INSERT INTO message_edits (message_id, old_text, new_text, edited_at)
        VALUES ($1, $2, $3, $4)
        """,
        message_id,
        old_text,
        new_text,
        edited_at,
    )


async def update_message_text(
    conn: asyncpg.Connection, message_id: UUID, new_text: str | None
) -> None:
    """Overwrite a message's current text after an edit (history kept separately)."""
    await conn.execute(
        "UPDATE messages SET message_text = $2 WHERE id = $1",
        message_id,
        new_text,
    )


async def mark_message_deleted(
    conn: asyncpg.Connection,
    *,
    chat_id: UUID,
    telegram_message_id: int,
    deletion_payload: dict[str, Any] | None,
) -> int:
    """Soft-delete a stored message: stamp ``deleted_at`` + the raw payload.

    Used by the Telegram Business ``deleted_business_messages`` handler (migration
    0006): a partner can delete a message on their side, and we keep the row but
    record that it was deleted (deletion in a partner chat can itself be a risk
    signal). Returns the number of rows updated — ``0`` when we never stored that
    message (e.g. it predates the chat being linked), so the caller can tell how
    many of a batch actually existed. Idempotent: re-deleting just rewrites
    ``deleted_at``.
    """
    result = await conn.execute(
        """
        UPDATE messages
        SET deleted_at = now(), deletion_payload = $3
        WHERE chat_id = $1 AND telegram_message_id = $2
        """,
        chat_id,
        telegram_message_id,
        deletion_payload,
    )
    # asyncpg execute returns a tag like "UPDATE 1"; take the trailing count.
    return int(result.split()[-1]) if result else 0


async def update_message_triggers(
    conn: asyncpg.Connection,
    message_id: UUID,
    *,
    has_triggers: bool,
    base_score: int,
    triggered_patterns: dict[str, Any] | None,
) -> None:
    """Write Tier-1 matcher output back onto a message (CLAUDE.md 7.1 step 7).

    ``triggered_patterns`` is passed natively for the pool's jsonb codec.
    """
    await conn.execute(
        """
        UPDATE messages
        SET has_triggers = $2,
            base_score = $3,
            triggered_patterns = $4
        WHERE id = $1
        """,
        message_id,
        has_triggers,
        base_score,
        triggered_patterns,
    )
