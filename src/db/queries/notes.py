"""notes queries. Migration 0006.

Internal-staff notes per partner / chat (general, handoff, open_question). Each
helper takes an already-acquired ``asyncpg.Connection`` (project-wide
convention; see ``chats.py``).
"""

from __future__ import annotations

from typing import Any
from uuid import UUID

import asyncpg

from src.db.models import Note


async def create(conn: asyncpg.Connection, payload: Note) -> Note:
    """Insert a new note; server fills ``id`` / ``created_at``."""
    row = await conn.fetchrow(
        """
        INSERT INTO notes (partner_id, chat_id, note_type, content, created_by)
        VALUES ($1, $2, $3, $4, $5)
        RETURNING *
        """,
        payload.partner_id,
        payload.chat_id,
        payload.note_type,
        payload.content,
        payload.created_by,
    )
    assert row is not None  # INSERT ... RETURNING always yields a row
    return Note.from_record(row)


async def list_by_partner(
    conn: asyncpg.Connection,
    partner_id: UUID,
    note_type: str | None = None,
    only_unresolved: bool = False,
) -> list[Note]:
    """Return a partner's notes, newest first, optionally filtered.

    ``note_type`` narrows to one kind; ``only_unresolved`` keeps just open notes
    (``resolved_at IS NULL``). Both filters are parameterized.
    """
    clauses = ["partner_id = $1"]
    params: list[Any] = [partner_id]
    if note_type is not None:
        params.append(note_type)
        clauses.append(f"note_type = ${len(params)}")
    if only_unresolved:
        clauses.append("resolved_at IS NULL")
    rows = await conn.fetch(
        f"SELECT * FROM notes WHERE {' AND '.join(clauses)} ORDER BY created_at DESC",
        *params,
    )
    return [Note.from_record(row) for row in rows]


async def resolve(
    conn: asyncpg.Connection, note_id: UUID, resolved_by: UUID
) -> Note | None:
    """Mark a note resolved. Returns the row, or ``None`` if already resolved/absent.

    Guarded to ``resolved_at IS NULL`` so a double-resolve is a no-op.
    """
    row = await conn.fetchrow(
        """
        UPDATE notes
        SET resolved_at = now(), resolved_by = $2
        WHERE id = $1 AND resolved_at IS NULL
        RETURNING *
        """,
        note_id,
        resolved_by,
    )
    return Note.from_record(row) if row is not None else None


async def get_by_short_id(conn: asyncpg.Connection, short_id: str) -> Note | None:
    """Look a note up by the first 8 chars of its UUID (for terse DM commands)."""
    row = await conn.fetchrow(
        "SELECT * FROM notes WHERE left(id::text, 8) = $1 LIMIT 1",
        short_id,
    )
    return Note.from_record(row) if row is not None else None
