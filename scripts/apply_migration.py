"""Apply one or more migration files to the live database.

    python scripts/apply_migration.py supabase/migrations/0023_archive_import.sql
    python scripts/apply_migration.py --verify 0023 0024

``schema_migrations`` is not maintained in this project (CLAUDE.md §14), so nothing
here records what ran — the files are written to be idempotent and each manages its
own transaction with ``BEGIN`` / ``COMMIT``. That means the statement text is sent
as-is via the simple query protocol rather than wrapped in another transaction, which
would nest the file's own ``BEGIN``.

``--verify`` re-checks that the objects a migration claims to create actually exist,
so "it ran" and "it took effect" are separate assertions.
"""

from __future__ import annotations

import argparse
import asyncio
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from src.db.client import acquire_connection, close_pool  # noqa: E402

#: What each migration must have produced, checked after it runs.
_EXPECTED: dict[str, dict[str, list[str]]] = {
    "0023": {
        "columns": ["chats.source", "chats.import_aff_id"],
        "functions": ["archive_placeholder_chat_id", "purge_old_data"],
        "indexes": [
            "idx_chats_import_aff_id",
            "idx_chats_source_status",
            "idx_messages_imported",
        ],
    },
    "0024": {
        "tables": ["archive_retro_findings", "archive_retro_progress"],
        "indexes": [
            "idx_retro_findings_run",
            "idx_retro_findings_chat",
            "idx_retro_findings_type",
            "idx_retro_findings_unique",
        ],
    },
}


async def _apply(paths: list[Path]) -> None:
    async with acquire_connection() as conn:
        for path in paths:
            sql = path.read_text(encoding="utf-8")
            print(f"applying {path.name} ({len(sql)} chars) …")
            await conn.execute(sql)
            print(f"  ok: {path.name}")


async def _verify(keys: list[str]) -> bool:
    ok = True
    async with acquire_connection() as conn:
        for key in keys:
            expected = _EXPECTED.get(key)
            if expected is None:
                print(f"{key}: no expectations declared — skipped")
                continue
            print(f"=== {key} ===")
            for table in expected.get("tables", []):
                found = await conn.fetchval(
                    "SELECT count(*) = 1 FROM information_schema.tables "
                    "WHERE table_schema='public' AND table_name=$1",
                    table,
                )
                print(f"  table  {table:<32}{'OK' if found else 'MISSING'}")
                ok &= bool(found)
            for ref in expected.get("columns", []):
                table, column = ref.split(".")
                found = await conn.fetchval(
                    "SELECT count(*) = 1 FROM information_schema.columns "
                    "WHERE table_schema='public' AND table_name=$1 AND column_name=$2",
                    table,
                    column,
                )
                print(f"  column {ref:<32}{'OK' if found else 'MISSING'}")
                ok &= bool(found)
            for function in expected.get("functions", []):
                found = await conn.fetchval(
                    "SELECT count(*) >= 1 FROM pg_proc WHERE proname = $1", function
                )
                print(f"  func   {function:<32}{'OK' if found else 'MISSING'}")
                ok &= bool(found)
            for index in expected.get("indexes", []):
                found = await conn.fetchval(
                    "SELECT count(*) = 1 FROM pg_indexes "
                    "WHERE schemaname='public' AND indexname=$1",
                    index,
                )
                print(f"  index  {index:<32}{'OK' if found else 'MISSING'}")
                ok &= bool(found)
    return ok


async def _main(args: argparse.Namespace) -> int:
    try:
        if args.verify:
            return 0 if await _verify(args.targets) else 1
        paths = [Path(t) for t in args.targets]
        missing = [str(p) for p in paths if not p.is_file()]
        if missing:
            sys.exit(f"not found: {', '.join(missing)}")
        await _apply(paths)
        return 0
    finally:
        await close_pool()


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("targets", nargs="+", help="SQL paths, or migration keys with --verify")
    parser.add_argument("--verify", action="store_true", help="check objects exist")
    parsed = parser.parse_args()
    raise SystemExit(asyncio.run(_main(parsed)))
