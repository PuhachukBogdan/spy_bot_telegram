"""Writing the archive into the database.

Everything here is idempotent, because a 57k-row import over a flaky link has to be
safely re-runnable:

* messages ride ``ON CONFLICT (chat_id, telegram_message_id) DO NOTHING``;
* archive units are keyed on a deterministic placeholder chat id, so a second run
  finds the same row instead of creating a twin;
* ``chat_events`` has no natural key, so events are cleared per chat before being
  re-inserted rather than accumulating duplicates.

Imported rows differ from live ones in two ways that are deliberate, not gaps:

``sender_id`` stays NULL — the export carries no user ids at all, so there is
nothing truthful to put there. ``sender_role`` therefore comes from the behavioural
roster (see :mod:`src.importer.roster`), and ``raw_payload`` holds the import
provenance rather than a Telegram object, because no Telegram object ever existed
for these rows.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any
from uuid import UUID

import asyncpg

from src.importer.matcher import MatchPlan
from src.importer.parser import ParsedExport, ParsedMessage
from src.utils.language import detect_language
from src.utils.logging import get_logger

log = get_logger(__name__)

#: Rows per executemany batch. Large enough to keep the round-trip count low,
#: small enough that one failure does not roll back a whole chat.
_BATCH = 500

#: Mirrors ``ingest._SIGNIFICANT_MIN_CHARS`` so imported and live rows are filtered
#: alike by the summary noise filter.
_SIGNIFICANT_MIN_CHARS = 3

_INSERT_MESSAGE = """
INSERT INTO messages (
    telegram_message_id, chat_id, sender_id, sender_chat_id, sender_name,
    sender_role, message_text, message_type, timestamp,
    reply_to_message_id, links, mentions, detected_language,
    is_significant, source, raw_payload, created_at
)
VALUES ($1, $2, NULL, NULL, $3, $4, $5, $6, $7, $8, $9, $10, $11, $12,
        'imported', $13, $14)
ON CONFLICT (chat_id, telegram_message_id) DO NOTHING
"""

_UPSERT_ARCHIVE_CHAT = """
INSERT INTO chats (
    telegram_chat_id, chat_name, status, unit_type, source, import_aff_id,
    last_processed_at
)
VALUES (archive_placeholder_chat_id($1), $2, 'archived', 'group', 'imported', $1,
        now())
ON CONFLICT (telegram_chat_id, topic_key) DO UPDATE
    SET chat_name     = EXCLUDED.chat_name,
        import_aff_id = EXCLUDED.import_aff_id,
        source        = 'imported'
RETURNING id
"""


@dataclass
class ImportResult:
    """Counts for the run report."""

    chats_created: int = 0
    chats_reused: int = 0
    messages_inserted: int = 0
    messages_skipped: int = 0
    events_inserted: int = 0
    folders_skipped: int = 0
    errors: list[str] = field(default_factory=list)

    def merge(self, other: ImportResult) -> None:
        self.chats_created += other.chats_created
        self.chats_reused += other.chats_reused
        self.messages_inserted += other.messages_inserted
        self.messages_skipped += other.messages_skipped
        self.events_inserted += other.events_inserted
        self.folders_skipped += other.folders_skipped
        self.errors.extend(other.errors)


def _is_significant(message: ParsedMessage) -> bool:
    if message.message_type in ("voice", "video_note", "document"):
        return True
    return bool(message.text and len(message.text.strip()) >= _SIGNIFICANT_MIN_CHARS)


def _provenance(export: ParsedExport, message: ParsedMessage) -> dict[str, Any]:
    """``raw_payload`` for an imported row: what the export actually said.

    Deliberately not shaped like a Telegram ``Message``. Faking one would invite
    code that reads ``raw_payload`` as a real payload — the transcription worker
    already does exactly that (``raw.get(message.message_type)``) — and would make
    an imported row indistinguishable from a live one at the point where the
    difference matters most.
    """
    payload: dict[str, Any] = {
        "import": {
            "aff_id": export.aff_id,
            "chat_title": export.chat_title,
        }
    }
    detail = payload["import"]
    if message.media_paths:
        detail["media"] = [{"path": path} for path in message.media_paths]
    if message.media_omitted:
        # The export references media it was not asked to download; recording this
        # stops a later reader concluding the message had no attachment.
        detail["media_omitted"] = True
    if message.is_forward:
        detail["forward"] = {"from_name": message.forward_from_name}
    return payload


async def _resolve_chat_id(
    conn: asyncpg.Connection, plan: MatchPlan
) -> tuple[UUID, bool]:
    """Return ``(chat_id, created)`` for a plan's destination."""
    if plan.target is not None:
        return plan.target.id, False
    chat_id: UUID = await conn.fetchval(
        _UPSERT_ARCHIVE_CHAT, plan.export.aff_id, plan.export.chat_title
    )
    return chat_id, True


async def _insert_messages(
    conn: asyncpg.Connection, chat_id: UUID, export: ParsedExport
) -> tuple[int, int]:
    """Insert every parsed message; return ``(inserted, skipped)``.

    ``executemany`` gives no per-row rowcount, so the delta in the chat's imported
    row count is used instead — it is exact and costs one extra query per chat.
    """
    before: int = await conn.fetchval(
        "SELECT count(*) FROM messages WHERE chat_id = $1 AND source = 'imported'",
        chat_id,
    )

    rows = [
        (
            message.telegram_message_id,
            chat_id,
            message.sender_name,
            message.sender_role,
            message.text,
            message.message_type,
            message.timestamp,
            message.reply_to_message_id,
            list(message.links),
            list(message.mentions),
            detect_language(message.text),
            _is_significant(message),
            _provenance(export, message),
            # created_at is the analysis cursor, so imported rows are dated when the
            # conversation happened, not when the import ran. That keeps them below
            # every existing watermark — defence in depth behind the source filter
            # in get_chat_analysis_window — and makes cursor order chronological.
            message.timestamp,
        )
        for message in export.messages
    ]

    for start in range(0, len(rows), _BATCH):
        await conn.executemany(_INSERT_MESSAGE, rows[start : start + _BATCH])

    after: int = await conn.fetchval(
        "SELECT count(*) FROM messages WHERE chat_id = $1 AND source = 'imported'",
        chat_id,
    )
    inserted = after - before
    return inserted, len(rows) - inserted


async def _insert_events(
    conn: asyncpg.Connection, chat_id: UUID, export: ParsedExport
) -> int:
    """Replace this chat's imported events (they have no natural key to dedup on)."""
    if not export.events:
        return 0
    await conn.execute(
        """
        DELETE FROM chat_events
         WHERE chat_id = $1 AND event_type = 'archive_import'
        """,
        chat_id,
    )
    await conn.executemany(
        """
        INSERT INTO chat_events (chat_id, event_type, payload)
        VALUES ($1, 'archive_import', $2)
        """,
        [
            (
                chat_id,
                {
                    "telegram_message_id": event.telegram_message_id,
                    "text": event.text,
                    "aff_id": export.aff_id,
                },
            )
            for event in export.events
        ],
    )
    return len(export.events)


async def apply_plan(conn: asyncpg.Connection, plan: MatchPlan) -> ImportResult:
    """Import one folder. Wrapped in its own transaction by the caller."""
    result = ImportResult()

    # Covers skip-duplicate and skip-excluded alike, so a new skip reason added to
    # the matcher cannot silently start importing here.
    if plan.action.startswith("skip-") or not plan.export.messages:
        result.folders_skipped = 1
        return result

    chat_id, created = await _resolve_chat_id(conn, plan)
    if created:
        result.chats_created = 1
    else:
        result.chats_reused = 1

    inserted, skipped = await _insert_messages(conn, chat_id, plan.export)
    result.messages_inserted = inserted
    result.messages_skipped = skipped
    result.events_inserted = await _insert_events(conn, chat_id, plan.export)

    log.info(
        "archive.folder_imported",
        aff_id=plan.export.aff_id,
        chat_id=str(chat_id),
        action=plan.action,
        inserted=inserted,
        skipped=skipped,
        events=result.events_inserted,
    )
    return result


async def apply_all(
    pool_acquire: Any, plans: list[MatchPlan], *, stop_on_error: bool = False
) -> ImportResult:
    """Import every plan, one transaction per folder.

    Per-folder transactions on purpose: a single 56k-row transaction would hold one
    connection for minutes and lose all progress to one bad row, while the
    idempotent inserts make a partial run safe to simply repeat.
    """
    total = ImportResult()
    for index, plan in enumerate(plans, start=1):
        try:
            async with pool_acquire() as conn, conn.transaction():
                total.merge(await apply_plan(conn, plan))
        except (asyncpg.PostgresError, OSError) as exc:
            message = f"{plan.export.aff_id}: {type(exc).__name__}: {exc}"
            total.errors.append(message)
            log.warning("archive.folder_failed", aff_id=plan.export.aff_id, error=str(exc))
            if stop_on_error:
                break
        if index % 25 == 0:
            log.info(
                "archive.progress",
                done=index,
                total=len(plans),
                inserted=total.messages_inserted,
            )
    return total
