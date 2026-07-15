"""Purge monitoring data older than a retention window (default 120 days ≈ 4 months).

Thin trigger over the DB-side ``purge_old_data(retention_days)`` function
(migration 0021). Use for a manual purge or from OS cron on the app server;
if you enable pg_cron the DB schedules itself and this script is just a fallback.

    python scripts/cleanup_retention.py                 # purge, 120-day window
    python scripts/cleanup_retention.py --days 90        # 3-month window
    python scripts/cleanup_retention.py --dry-run        # count only, delete nothing

--dry-run counts rows older than the window per driver table inside a transaction
that is always rolled back, so nothing is removed.
"""

from __future__ import annotations

import argparse
import asyncio
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from src.db.client import acquire_connection, close_pool  # noqa: E402

# Driver tables counted in --dry-run (the growth contributors keyed on created_at).
_DRY_RUN_TABLES = (
    "messages",
    "risk_events",
    "llm_calls",
    "processing_queue",
    "message_edits",
    "analyzed_file_hashes",
    "activity_signals",
    "chat_events",
    "admin_audit_log",
)


async def _dry_run(days: int) -> None:
    async with acquire_connection() as conn:
        tx = conn.transaction()
        await tx.start()
        try:
            print(f"DRY RUN — rows older than {days} days (nothing will be deleted):")
            total = 0
            for table in _DRY_RUN_TABLES:
                n = await conn.fetchval(
                    f"select count(*) from {table} "
                    "where created_at < now() - make_interval(days => $1)",
                    days,
                )
                total += n
                print(f"  {table:<22} {n:>8}")
            print(f"  {'TOTAL':<22} {total:>8}")
        finally:
            await tx.rollback()


async def _purge(days: int) -> None:
    async with acquire_connection() as conn:
        rows = await conn.fetch("select * from purge_old_data($1)", days)
        print(f"Purged rows older than {days} days:")
        total = 0
        for r in rows:
            total += r["deleted"]
            print(f"  {r['table_name']:<22} {r['deleted']:>8}")
        print(f"  {'TOTAL':<22} {total:>8}")


async def _run(days: int, dry_run: bool) -> None:
    try:
        if dry_run:
            await _dry_run(days)
        else:
            await _purge(days)
    finally:
        await close_pool()


if __name__ == "__main__":
    ap = argparse.ArgumentParser(description="Purge monitoring data past a retention window.")
    ap.add_argument("--days", type=int, default=120, help="retention window in days (default 120)")
    ap.add_argument("--dry-run", action="store_true", help="count only, delete nothing")
    args = ap.parse_args()
    if args.days < 1:
        sys.exit("--days must be >= 1")
    asyncio.run(_run(args.days, args.dry_run))
