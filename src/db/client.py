"""asyncpg connection pool. Phase 2.

A single module-level pool, created lazily from
``settings.SUPABASE_DB_URL`` and shared across all workers in the process
(CLAUDE.md section 10: one Python process, no global state except ``settings``;
the pool is the one sanctioned shared resource).

Usage::

    from src.db.client import acquire_connection

    async with acquire_connection() as conn:
        rows = await conn.fetch("SELECT 1")

Call ``close_pool()`` once on graceful shutdown.
"""

from __future__ import annotations

import asyncio
import json
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

import asyncpg

from src.config import settings
from src.utils.logging import get_logger

log = get_logger(__name__)

# Hardcoded for now; promote to .env later if tuning is needed.
_POOL_MIN_SIZE = 2
_POOL_MAX_SIZE = 10

_pool: asyncpg.Pool | None = None
# Guards lazy creation so concurrent first callers don't each build a pool
# (the ``await create_pool`` suspension point makes a naive check-then-set racy).
_pool_lock = asyncio.Lock()


async def _init_connection(conn: asyncpg.Connection) -> None:
    """Per-connection setup run by the pool on every new connection.

    asyncpg leaves ``json`` / ``jsonb`` columns as raw text by default. Without
    this codec, ``messages.raw_payload``, ``internal_users.telegram_accounts``,
    ``processing_queue.payload`` etc. come back as ``str`` and break the Pydantic
    row models (a ``list[int]`` field handed a string fails validation). Encoding
    with ``json.dumps`` also lets callers pass native dict/list params without a
    manual ``$n::jsonb`` cast.
    """
    for typename in ("json", "jsonb"):
        await conn.set_type_codec(
            typename,
            encoder=json.dumps,
            decoder=json.loads,
            schema="pg_catalog",
        )


async def get_pool() -> asyncpg.Pool:
    """Return the process-wide pool, creating it on first call (double-checked)."""
    global _pool
    if _pool is None:
        async with _pool_lock:
            if _pool is None:
                log.info(
                    "db.pool.create",
                    min_size=_POOL_MIN_SIZE,
                    max_size=_POOL_MAX_SIZE,
                )
                _pool = await asyncpg.create_pool(
                    dsn=settings.SUPABASE_DB_URL.get_secret_value(),
                    min_size=_POOL_MIN_SIZE,
                    max_size=_POOL_MAX_SIZE,
                    init=_init_connection,
                )
    return _pool


@asynccontextmanager
async def acquire_connection() -> AsyncIterator[asyncpg.Connection]:
    """Acquire a connection from the pool, releasing it on exit."""
    pool = await get_pool()
    async with pool.acquire() as conn:
        yield conn


async def close_pool() -> None:
    """Close the pool on graceful shutdown. Idempotent."""
    global _pool
    if _pool is not None:
        await _pool.close()
        _pool = None
        log.info("db.pool.closed")
