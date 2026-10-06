"""Run the chat-membership sweep once, or show what the table currently says.

    python scripts/sweep_membership.py --run        # ask Telegram, write chat_members + titles
    python scripts/sweep_membership.py --status     # read-only: presence per person/account

``--run`` is the same pass the in-process worker makes every
``MEMBERSHIP_SWEEP_INTERVAL_SECONDS`` (see ``src/pipeline/membership.py``): one
``getChat`` per active group (title refresh) and one ``getChatMember`` per staff
account per group. All READ calls against Telegram; the only writes are to our
own ``chat_members`` and ``chats.chat_name``. Needs ``TELEGRAM_BOT_TOKEN`` and
``SUPABASE_DB_URL`` in ``.env`` — the live bot token works from anywhere, it is
not tied to the webhook.
"""

from __future__ import annotations

import argparse
import asyncio
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from aiogram import Bot  # noqa: E402

from src.config import settings  # noqa: E402
from src.db.client import acquire_connection, close_pool  # noqa: E402
from src.db.queries.chat_members import (  # noqa: E402
    count_present_by_account,
    list_staff_accounts,
)
from src.pipeline.membership import run_membership_sweep  # noqa: E402


async def _status() -> None:
    async with acquire_connection() as conn:
        accounts = await list_staff_accounts(conn)
        present = await count_present_by_account(conn)
        stale = await conn.fetchval(
            """
            WITH last_t AS (
              SELECT DISTINCT ON (chat_id) chat_id, payload->>'new_title' AS new_title
              FROM chat_events WHERE event_type='title_change'
              ORDER BY chat_id, created_at DESC)
            SELECT count(*) FROM last_t t JOIN chats c ON c.id = t.chat_id
            WHERE c.status='active' AND c.chat_name IS DISTINCT FROM t.new_title
            """
        )
        verified = await conn.fetchrow(
            "SELECT count(*) AS rows, max(last_verified_at) AS last_verified, "
            "count(*) FILTER (WHERE source='sweep') AS swept FROM chat_members"
        )
    print(
        f"chat_members rows={verified['rows']} swept={verified['swept']} "
        f"last_verified={verified['last_verified']}  titles still stale vs events={stale}"
    )
    print(f"{'person':28s} {'role':8s} {'account':12s} {'chats':>5s}  flags")
    for a in accounts:
        flags = []
        if not a.enabled:
            flags.append("disabled")
        if a.deactivated_at is not None:
            flags.append(f"deactivated {a.deactivated_at:%Y-%m-%d}")
        print(
            f"{a.full_name[:28]:28s} {a.role:8s} {a.telegram_user_id:<12d} "
            f"{present.get(a.telegram_user_id, 0):5d}  {', '.join(flags)}"
        )


async def _run(concurrency: int | None) -> None:
    bot = Bot(token=settings.TELEGRAM_BOT_TOKEN.get_secret_value())
    try:
        stats = await run_membership_sweep(bot, concurrency=concurrency)
    finally:
        await bot.session.close()
    print(
        f"chats={stats.chats} unreachable={stats.chats_unreachable} "
        f"titles_updated={stats.titles_updated} calls={stats.calls} "
        f"present={stats.present} absent={stats.absent} errors={stats.errors}"
    )
    if stats.unreachable:
        print("unreachable (bot not in the chat any more?):", stats.unreachable)


async def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--run", action="store_true", help="run one sweep now")
    parser.add_argument("--status", action="store_true", help="print presence per account")
    parser.add_argument("--concurrency", type=int, default=None)
    args = parser.parse_args()
    if not args.run and not args.status:
        parser.error("choose --run and/or --status")
    try:
        if args.run:
            await _run(args.concurrency)
        if args.status:
            await _status()
    finally:
        await close_pool()


if __name__ == "__main__":
    asyncio.run(main())
