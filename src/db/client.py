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


def _parse_dsn(dsn: str) -> dict[str, object]:
    """Parse a PostgreSQL DSN into keyword args for asyncpg.create_pool.

    Uses rfind('@') as the user:password / host boundary so passwords that
    contain literal '@' characters are handled correctly (plain urlparse splits
    at the first '@' and produces a broken host/port).
    """
    _scheme, rest = dsn.split("://", 1)
    at = rest.rfind("@")
    userinfo, hostinfo = rest[:at], rest[at + 1 :]

    colon = userinfo.find(":")
    user = userinfo[:colon]
    password = userinfo[colon + 1 :]

    if "/" in hostinfo:
        hostport, database = hostinfo.split("/", 1)
    else:
        hostport, database = hostinfo, "postgres"

    if ":" in hostport:
        host, port_str = hostport.rsplit(":", 1)
        port = int(port_str)
    else:
        host, port = hostport, 5432

    return {"host": host, "port": port, "user": user, "password": password, "database": database}


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
                dsn_kwargs = _parse_dsn(settings.SUPABASE_DB_URL.get_secret_value())
                if settings.SUPABASE_DB_PASSWORD is not None:
                    dsn_kwargs["password"] = settings.SUPABASE_DB_PASSWORD.get_secret_value()
                _pool = await asyncpg.create_pool(
                    **dsn_kwargs,
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
