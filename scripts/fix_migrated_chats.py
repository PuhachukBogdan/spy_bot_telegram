"""Repair units stranded by a failed group->supergroup migration repoint.

Fixes the data half of the migration race (the code half is
``chats.migrate_chat_telegram_id``, which now parks the duplicate instead of
raising on the unique constraint).

**What went wrong.** A migration emits two Telegram updates and they race.
``my_chat_member`` for the new supergroup normally lands first, so
``on_bot_added`` creates a unit at the NEW id — attributed to whoever triggered
the migration, i.e. the partner who owns the group, so it lands ``'pending'``.
The ``migrate_to_chat_id`` service message then tries to repoint the real unit and
hits ``UNIQUE (telegram_chat_id, topic_key)``. The event insert had already
committed on its own connection, so the wreckage looks like a *successful*
migration: a ``migration`` event on a unit that never moved.

**Why it was invisible.** The stranded unit keeps ``status='active'``, so
``/chats``, the weekly/monthly reports and the daily digest all show a healthy
chat — while Telegram routes that chat's traffic to an id the bot now treats as a
different, ``'pending'`` chat. Seven days later the abandoned-chat sweep marks the
duplicate ``'abandoned'`` and the bot LEAVES. Net effect: the partner is
completely unmonitored and every surface still says otherwise.

**Why re-adding the bot is not enough on its own.** ``create_active_chat`` is
``ON CONFLICT (telegram_chat_id, topic_key) DO NOTHING``, so a re-add finds the
abandoned duplicate already holding the new id, returns ``None`` ("already
known"), and the unit stays ``'abandoned'`` — the whitelist middleware then drops
every message. The rows have to be repaired first; after that a re-add by a
whitelisted manager is a no-op that just works.

Dry-run by default — prints the exact changes and writes nothing::

    python scripts/fix_migrated_chats.py            # report only
    python scripts/fix_migrated_chats.py --apply    # perform the repair
"""

from __future__ import annotations

import asyncio
import json
import sys
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import asyncpg  # noqa: E402

from src.db.client import acquire_connection, close_pool  # noqa: E402
from src.db.queries.chats import migrate_chat_telegram_id  # noqa: E402

#: Every recorded migration, newest last. Read from ``chat_events`` rather than a
#: hardcoded list so the script stays correct if more migrations happen before it
#: is run.
_MIGRATIONS_SQL = """
    SELECT e.created_at, e.payload
    FROM chat_events e
    WHERE e.event_type = 'migration'
    ORDER BY e.created_at
"""


async def _load_broken(conn: asyncpg.Connection) -> list[dict[str, Any]]:
    """Return migrations whose repoint never landed, newest last."""
    broken: list[dict[str, Any]] = []
    for event in await conn.fetch(_MIGRATIONS_SQL):
        payload = event["payload"]
        if isinstance(payload, str):  # jsonb may arrive as text
            payload = json.loads(payload)
        old_id, new_id = payload.get("old_chat_id"), payload.get("new_chat_id")
        if old_id is None or new_id is None:
            continue

        # Broken == the old id still holds a live unit. A completed migration
        # leaves nothing there, so this is the whole test.
        survivors = await conn.fetch(
            "SELECT id, chat_name, status, topic_key FROM chats "
            "WHERE telegram_chat_id = $1 AND status <> 'merged' ORDER BY topic_key",
            old_id,
        )
        if not survivors:
            continue
        duplicates = await conn.fetch(
            "SELECT id, chat_name, status, topic_key, added_by_user_id FROM chats "
            "WHERE telegram_chat_id = $1 ORDER BY topic_key",
            new_id,
        )
        broken.append(
            {
                "at": event["created_at"],
                "old_id": old_id,
                "new_id": new_id,
                "survivors": survivors,
                "duplicates": duplicates,
            }
        )
    return broken


def _report(broken: list[dict[str, Any]]) -> None:
    if not broken:
        print("No stranded migrations. Nothing to repair.")
        return
    print(f"{len(broken)} migration(s) never completed their repoint:\n")
    for item in broken:
        name = item["survivors"][0]["chat_name"] or "(no title)"
        print(f"  '{name}'   migrated {item['at']:%Y-%m-%d %H:%M} UTC")
        print(f"    old id {item['old_id']}  ->  new id {item['new_id']}")
        for row in item["survivors"]:
            print(
                f"      KEEP  topic_key={row['topic_key']} status={row['status']:10}"
                f" -> moves to {item['new_id']}"
            )
        for row in item["duplicates"]:
            fate = (
                "parked on the old id as 'merged'"
                if any(s["topic_key"] == row["topic_key"] for s in item["survivors"])
                else "left alone (no collision)"
            )
            print(
                f"      DUP   topic_key={row['topic_key']} status={row['status']:10}"
                f" added_by={row['added_by_user_id']} -> {fate}"
            )
        print()


async def _apply(broken: list[dict[str, Any]]) -> None:
    for item in broken:
        name = item["survivors"][0]["chat_name"] or "(no title)"
        async with acquire_connection() as conn, conn.transaction():
            outcome = await migrate_chat_telegram_id(
                conn, item["old_id"], item["new_id"]
            )
        print(
            f"  '{name}': moved {outcome.units_moved} unit(s), "
            f"parked {outcome.units_parked} duplicate(s)"
        )


async def _verify(broken: list[dict[str, Any]]) -> bool:
    ok = True
    async with acquire_connection() as conn:
        for item in broken:
            live = await conn.fetchval(
                "SELECT count(*) FROM chats WHERE telegram_chat_id = $1 "
                "AND status = 'active'",
                item["new_id"],
            )
            stale = await conn.fetchval(
                "SELECT count(*) FROM chats WHERE telegram_chat_id = $1 "
                "AND status <> 'merged'",
                item["old_id"],
            )
            name = item["survivors"][0]["chat_name"] or "(no title)"
            good = int(live) > 0 and int(stale) == 0
            ok = ok and good
            print(
                f"  {'OK  ' if good else 'FAIL'} '{name}': "
                f"active units at new id={live}, live units left at old id={stale}"
            )
    return ok


async def _run(*, apply: bool) -> None:
    try:
        async with acquire_connection() as conn:
            broken = await _load_broken(conn)

        _report(broken)
        if not broken:
            return
        if not apply:
            print("Dry run — nothing written. Re-run with --apply to repair.")
            return

        print("Applying …")
        await _apply(broken)
        print("\nVerifying …")
        ok = await _verify(broken)

        print(
            "\nDone. NOTE: the bot was removed from these chats when the duplicate\n"
            "was swept, so repairing the rows does not by itself restore monitoring.\n"
            "A whitelisted manager must re-add the bot to each chat; onboarding will\n"
            "then find the repaired active unit and simply continue."
        )
        if not ok:
            sys.exit("verification failed — see FAIL rows above")
    finally:
        await close_pool()


if __name__ == "__main__":
    flags = set(sys.argv[1:])
    unknown = flags - {"--apply"}
    if unknown:
        sys.exit(
            f"usage: python scripts/fix_migrated_chats.py [--apply]  (got {unknown})"
        )
    asyncio.run(_run(apply="--apply" in flags))
