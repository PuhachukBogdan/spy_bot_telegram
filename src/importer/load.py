"""Database reads the archive importer needs (no writes live here)."""

from __future__ import annotations

from uuid import UUID

import asyncpg

from src.importer.matcher import DbChat


async def has_import_columns(conn: asyncpg.Connection) -> bool:
    """Whether migration 0023 has been applied to ``chats``."""
    return bool(
        await conn.fetchval(
            """
            SELECT count(*) = 2 FROM information_schema.columns
            WHERE table_schema = 'public' AND table_name = 'chats'
              AND column_name IN ('source', 'import_aff_id')
            """
        )
    )


async def load_db_chats(conn: asyncpg.Connection) -> list[DbChat]:
    """Every monitored unit, with the live message count the matcher ranks on.

    Includes non-active rows on purpose: duplicates left by re-onboarding (``76812``
    is both ``active`` and ``pending``) have to be *visible* to be ranked, and an
    ``archived`` unit from a previous import run must be found so a re-run updates it
    instead of creating a second one.

    Works on both sides of migration 0023. That is not defensiveness for its own
    sake: the dry-run report exists to be read *before* the schema is changed, so it
    must not require the columns it is helping to justify.
    """
    if await has_import_columns(conn):
        provenance = "COALESCE(c.source, 'live') AS source, c.import_aff_id"
    else:
        provenance = "'live'::text AS source, NULL::text AS import_aff_id"

    rows = await conn.fetch(
        f"""
        SELECT c.id, c.telegram_chat_id, c.chat_name, c.status, c.unit_type,
               c.last_processed_at,
               {provenance},
               (SELECT count(*) FROM messages m
                 WHERE m.chat_id = c.id AND m.source <> 'imported') AS message_count
        FROM chats c
        ORDER BY c.chat_name
        """  # noqa: S608 - interpolation is a fixed column list, not user input
    )
    return [
        DbChat(
            id=row["id"],
            telegram_chat_id=row["telegram_chat_id"],
            chat_name=row["chat_name"],
            status=row["status"],
            unit_type=row["unit_type"],
            message_count=row["message_count"],
            last_processed_at=row["last_processed_at"],
            source=row["source"],
            import_aff_id=row["import_aff_id"],
        )
        for row in rows
    ]


async def load_existing_message_ids(
    conn: asyncpg.Connection, chat_ids: list[UUID]
) -> dict[UUID, frozenset[int]]:
    """``telegram_message_id`` already stored per chat, for collision reporting.

    The archive overlaps the live window — 13 763 of its messages are dated on or
    after the bot started — so a matched chat can already hold rows the export also
    carries. ``UNIQUE (chat_id, telegram_message_id)`` makes the import idempotent
    regardless; this only lets the dry run say how many rows will be no-ops.
    """
    if not chat_ids:
        return {}
    rows = await conn.fetch(
        """
        SELECT chat_id, array_agg(telegram_message_id) AS ids
        FROM messages
        WHERE chat_id = ANY($1::uuid[])
        GROUP BY chat_id
        """,
        chat_ids,
    )
    return {row["chat_id"]: frozenset(row["ids"]) for row in rows}
