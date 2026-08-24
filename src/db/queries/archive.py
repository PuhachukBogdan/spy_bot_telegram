"""Re-attaching imported history when the bot finally joins an archived group.

Roughly half the export folders had no ``chats`` row, so their history lives in
``status='archived'`` placeholder units keyed on ``import_aff_id``. When the bot is
later added to one of those groups, the history has to follow it into the real unit —
otherwise the chat starts from zero and every report and ``/risk`` lookup silently
misses months of context.

The move is deliberately non-destructive. Rows that cannot move (the real chat
already holds the same ``telegram_message_id``) are LEFT in the placeholder rather
than deleted: ``risk_events``, ``message_edits`` and ``analyzed_file_hashes`` all
carry NO ACTION foreign keys onto ``messages.id``, and the one-off retro analysis
does attach ``risk_events`` to imported rows — so deleting a "duplicate" could fail
on an FK, or worse, take a finding with it. Leaving them costs a few kB and keeps the
operation safe to repeat.
"""

from __future__ import annotations

from dataclasses import dataclass
from uuid import UUID

import asyncpg


@dataclass(frozen=True)
class ArchiveUnit:
    """An archive-only unit that could receive a real chat's identity."""

    id: UUID
    chat_name: str | None
    import_aff_id: str | None
    message_count: int


@dataclass(frozen=True)
class AttachResult:
    """Outcome of attaching one archived unit to a real chat."""

    source_chat_id: UUID
    target_chat_id: UUID
    messages_moved: int
    messages_left: int
    events_moved: int

    @property
    def moved_anything(self) -> bool:
        return bool(self.messages_moved or self.events_moved)


async def find_archived_unit_for_aff_ids(
    conn: asyncpg.Connection, aff_ids: list[str]
) -> ArchiveUnit | None:
    """Find the archived unit belonging to any of *aff_ids*.

    Prefers the unit holding the most history, so if a chat title carries several
    ids the richest archive wins rather than whichever row sorts first.
    """
    if not aff_ids:
        return None
    row = await conn.fetchrow(
        """
        SELECT c.id, c.chat_name, c.import_aff_id,
               (SELECT count(*) FROM messages m WHERE m.chat_id = c.id) AS message_count
        FROM chats c
        WHERE c.status = 'archived'
          AND c.source = 'imported'
          AND c.import_aff_id = ANY($1::text[])
        ORDER BY message_count DESC
        LIMIT 1
        """,
        aff_ids,
    )
    if row is None:
        return None
    return ArchiveUnit(
        id=row["id"],
        chat_name=row["chat_name"],
        import_aff_id=row["import_aff_id"],
        message_count=row["message_count"],
    )


async def attach_archived_history(
    conn: asyncpg.Connection, *, source_chat_id: UUID, target_chat_id: UUID
) -> AttachResult:
    """Move an archived unit's messages and events onto *target_chat_id*.

    Caller supplies the transaction. Idempotent: a second call moves nothing because
    the first one emptied the movable set.
    """
    moved: int = (
        await conn.fetchval(
            """
            WITH moved AS (
                UPDATE messages m
                   SET chat_id = $2
                 WHERE m.chat_id = $1
                   AND NOT EXISTS (
                        SELECT 1 FROM messages existing
                         WHERE existing.chat_id = $2
                           AND existing.telegram_message_id = m.telegram_message_id
                       )
                RETURNING 1
            )
            SELECT count(*) FROM moved
            """,
            source_chat_id,
            target_chat_id,
        )
        or 0
    )

    left: int = (
        await conn.fetchval(
            "SELECT count(*) FROM messages WHERE chat_id = $1", source_chat_id
        )
        or 0
    )

    events: int = (
        await conn.fetchval(
            """
            WITH moved AS (
                UPDATE chat_events SET chat_id = $2 WHERE chat_id = $1 RETURNING 1
            )
            SELECT count(*) FROM moved
            """,
            source_chat_id,
            target_chat_id,
        )
        or 0
    )

    # The placeholder is retired, not deleted: it is the only remaining record that
    # this history arrived from the archive, and it may still hold collided rows.
    await conn.execute(
        "UPDATE chats SET status = 'merged' WHERE id = $1", source_chat_id
    )

    return AttachResult(
        source_chat_id=source_chat_id,
        target_chat_id=target_chat_id,
        messages_moved=moved,
        messages_left=left,
        events_moved=events,
    )
