"""One-time backfill: attach each partner to its owning manager via chat-title aff_id.

Onboarding now derives manager ownership from the aff_id in the chat title (see
``get_or_create_manager_by_aff_id``), but the pilot's existing partners were bound
BEFORE that rule existed, so they all carry ``owner_manager_id = NULL`` — which makes
the manager-centric report empty (its query INNER-JOINs partners → internal_users).

This script re-parses each active group chat's title, derives/creates the manager
stub for its aff_id, and sets ``partners.owner_manager_id`` for any partner that is
still unowned. It uses the SAME parser and get-or-create helper as onboarding, so the
result is identical to what a fresh onboard would have produced. A later manager
registration enriches the same stub row (matched on aff_id) — nothing is thrown away.

Dry-run by DEFAULT (read-only). Pass --apply to write.

    python scripts/backfill_partner_owners.py            # dry-run, shows the plan
    python scripts/backfill_partner_owners.py --apply    # perform the updates
"""

from __future__ import annotations

import asyncio
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from src.bot.handlers.chat_member import _parse_chat_title  # noqa: E402
from src.db.client import close_pool, get_pool  # noqa: E402
from src.db.queries.etc import get_or_create_manager_by_aff_id  # noqa: E402

APPLY = "--apply" in sys.argv


async def _run() -> None:
    pool = await get_pool()
    updated = skipped_owned = skipped_noparse = skipped_noaff = 0
    async with pool.acquire() as conn:
        rows = await conn.fetch(
            """
            SELECT c.telegram_chat_id, c.chat_name,
                   p.id AS partner_id, p.name AS partner_name, p.owner_manager_id
            FROM chats c
            JOIN partners p ON p.id = c.partner_id
            WHERE c.status = 'active' AND c.unit_type = 'group'
            ORDER BY c.chat_name
            """
        )
        mode = "APPLY" if APPLY else "DRY-RUN"
        print(f"{mode} — {len(rows)} active group chat(s) with a partner\n")

        for r in rows:
            title = r["chat_name"] or ""
            label = f"  [{r['telegram_chat_id']}] {title!r} → partner {r['partner_name']!r}"

            if r["owner_manager_id"] is not None:
                print(f"{label}: already owned, skip")
                skipped_owned += 1
                continue

            parsed = _parse_chat_title(title)
            if parsed is None:
                print(f"{label}: title does not parse (no Beton.Win marker?), skip")
                skipped_noparse += 1
                continue

            aff_id, _pname = parsed
            if not aff_id:
                print(f"{label}: no numeric aff_id in title, skip")
                skipped_noaff += 1
                continue

            if not APPLY:
                print(f"{label}: WOULD set owner → manager(aff_id={aff_id})")
                updated += 1
                continue

            async with conn.transaction():
                mgr = await get_or_create_manager_by_aff_id(conn, aff_id)
                result = await conn.execute(
                    "UPDATE partners SET owner_manager_id = $1 "
                    "WHERE id = $2 AND owner_manager_id IS NULL",
                    mgr.id,
                    r["partner_id"],
                )
            print(f"{label}: set owner → manager(aff_id={aff_id}, id={mgr.id})  [{result}]")
            updated += 1

        print(
            f"\nSummary: {updated} {'to update' if not APPLY else 'updated'}, "
            f"{skipped_owned} already owned, "
            f"{skipped_noparse} unparseable, {skipped_noaff} no-aff_id"
        )
        if not APPLY and updated:
            print("Re-run with --apply to perform the updates.")

    await close_pool()


if __name__ == "__main__":
    asyncio.run(_run())
