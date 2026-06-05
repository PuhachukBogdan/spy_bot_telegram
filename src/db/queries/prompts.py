"""prompts queries. Phase 8.

Reads the active prompt template for a given name. The LLM-prompt resolver
(``src/llm/prompts.py``) falls back to ``prompts/<name>.txt`` when the DB template
is empty (the seed ships empty templates on purpose). Takes an already-acquired
``asyncpg.Connection`` (project-wide convention).
"""

from __future__ import annotations

import asyncpg

from src.db.models import Prompt


async def get_active_prompt(conn: asyncpg.Connection, name: str) -> Prompt | None:
    """Return the highest-version active prompt for ``name``, or ``None``.

    ``UNIQUE (name, version)`` means at most one row per version; ``active`` may be
    set on several, so the newest active version wins.
    """
    row = await conn.fetchrow(
        """
        SELECT * FROM prompts
        WHERE name = $1 AND active = true
        ORDER BY version DESC
        LIMIT 1
        """,
        name,
    )
    return Prompt.from_record(row) if row is not None else None
