"""Register missing internal staff and repair historical ``messages.sender_role``.

Fixes the data half of the staff-as-partner bug (the code half is in
``ingest._resolve_sender_role``, which now uses the enabled-agnostic lookup).

Two distinct data defects:

1. **Unregistered staff.** People who operate partner chats but were never added
   to ``internal_users``, so every message they sent was recorded as ``partner``.
   Identified from the archive's behavioural evidence — portfolio span plus
   invite/pin activity across many chats (see ``src.importer.roster``).
2. **Pre-registration history.** ``sender_role`` is resolved once at ingest time
   and frozen in the row, so messages received *before* a staff member was
   whitelisted keep ``partner`` forever. Christopher and Mirror | Betonwin were
   registered around 2026-07-17; everything earlier is mislabelled.

Both matter because the Tier-2 contract reasons in terms of "an internal employee
proposed X to a partner". An inverted role does not just mislabel a report column,
it inverts the premise the risk verdict rests on.

``sender_role`` is a *derived* attribute of who the sender is, not a record of a
decision anyone made, so correcting it in place is a data fix rather than a
rewrite of history. Nothing in ``risk_events`` is touched: the stored
``llm_explanation`` strings are a faithful record of what the model said at the
time, wrong premise included.

Dry-run by default — prints the exact changes and writes nothing::

    python scripts/fix_sender_roles.py            # report only
    python scripts/fix_sender_roles.py --apply    # perform the fix
"""

from __future__ import annotations

import asyncio
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import asyncpg  # noqa: E402

from src.db.client import acquire_connection, close_pool  # noqa: E402

#: Staff to whitelist, keyed by Telegram user id (taken from ``messages.sender_id``,
#: which ingestion records even when it misclassifies the role).
#:
#: ``enabled`` is the *access* decision and is intentionally separate from being
#: staff: Сhicco left the company on 2026-07-10, so they are registered as staff —
#: which fixes role attribution for their history — but stay disabled, granting no
#: bot access. The code fix is what makes that combination behave correctly.
MISSING_STAFF: dict[int, dict[str, object]] = {
    8422016171: {
        "full_name": "Geralt | Betonwin",
        "role": "manager",
        "enabled": True,
        "evidence": "present in 173 archive chats; pinned/administered 69; brand tag",
    },
    7884114267: {
        "full_name": "Сhicco| Betonwin",
        "role": "manager",
        "enabled": False,
        "evidence": "brand tag; departed 2026-07-10 (farewell message) — no bot access",
    },
}


async def _report(conn: asyncpg.Connection) -> None:
    print("=== staff to register ===")
    for tg_id, spec in MISSING_STAFF.items():
        existing = await conn.fetchrow(
            "SELECT full_name, enabled FROM internal_users WHERE telegram_accounts @> $1::jsonb",
            [tg_id],
        )
        state = "ALREADY PRESENT" if existing is not None else "will INSERT"
        print(f"  {tg_id:<12} {str(spec['full_name']):<22} enabled={spec['enabled']!s:<5} {state}")
        print(f"               evidence: {spec['evidence']}")

    print("\n=== messages that would be relabelled internal ===")
    rows = await conn.fetch(
        """
        SELECT m.sender_id, m.sender_name, count(*) AS n,
               count(DISTINCT m.chat_id) AS chats,
               min(m.timestamp) AS first_seen, max(m.timestamp) AS last_seen
        FROM messages m
        WHERE m.sender_role <> 'internal'
          AND (
                EXISTS (
                    SELECT 1 FROM internal_users u
                     WHERE u.telegram_accounts @> to_jsonb(m.sender_id)
                )
                OR m.sender_id = ANY($1::bigint[])
              )
        GROUP BY 1, 2
        ORDER BY n DESC
        """,
        list(MISSING_STAFF),
    )
    total = 0
    for row in rows:
        total += row["n"]
        print(
            f"  id={row['sender_id']:<12} {str(row['sender_name'])[:24]:<24} "
            f"{row['n']:>4} msgs in {row['chats']:>3} chats  "
            f"{row['first_seen']:%Y-%m-%d}..{row['last_seen']:%Y-%m-%d}"
        )
    print(f"  TOTAL: {total} rows")


async def _apply(conn: asyncpg.Connection) -> None:
    async with conn.transaction():
        for tg_id, spec in MISSING_STAFF.items():
            inserted = await conn.fetchval(
                """
                INSERT INTO internal_users (full_name, role, telegram_accounts, enabled)
                SELECT $1, $2, $3::jsonb, $4
                 WHERE NOT EXISTS (
                    SELECT 1 FROM internal_users WHERE telegram_accounts @> $3::jsonb
                 )
                RETURNING id
                """,
                spec["full_name"],
                spec["role"],
                [tg_id],
                spec["enabled"],
            )
            verb = "inserted" if inserted is not None else "already present, skipped"
            print(f"  {tg_id} {spec['full_name']}: {verb}")

        updated = await conn.fetchval(
            """
            WITH fixed AS (
                UPDATE messages m
                   SET sender_role = 'internal'
                 WHERE m.sender_role <> 'internal'
                   AND EXISTS (
                        SELECT 1 FROM internal_users u
                         WHERE u.telegram_accounts @> to_jsonb(m.sender_id)
                       )
                RETURNING 1
            )
            SELECT count(*) FROM fixed
            """
        )
        print(f"  messages relabelled internal: {updated}")


async def _run(apply: bool) -> None:
    try:
        async with acquire_connection() as conn:
            await _report(conn)
            if not apply:
                print("\nDRY RUN — nothing written. Re-run with --apply to perform the fix.")
                return
            print("\n=== applying ===")
            await _apply(conn)
            print("\ndone.")
    finally:
        await close_pool()


if __name__ == "__main__":
    flags = set(sys.argv[1:])
    unknown = flags - {"--apply"}
    if unknown:
        sys.exit(f"usage: python scripts/fix_sender_roles.py [--apply]  (got {unknown})")
    asyncio.run(_run(apply="--apply" in flags))
