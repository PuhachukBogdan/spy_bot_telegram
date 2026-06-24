"""Find and fix partners whose name is the placeholder 'Partner Name'.

For each such partner, look at the title of their monitored chats.
If the title matches the '{aff_id} | {partner_name} | Beton[.]Win' pattern,
update partners.name to the extracted partner_name.
Otherwise, print the chat title so you can decide manually.

    python scripts/_fix_partner_names.py [--dry-run]
"""

from __future__ import annotations

import asyncio
import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from src.db.client import close_pool, get_pool  # noqa: E402

_TITLE_RE = re.compile(
    r"^\s*(\S+)\s*\|\s*(.+?)\s*\|\s*beton\.?win\s*$",
    re.IGNORECASE,
)

DRY_RUN = "--dry-run" in sys.argv


async def _run() -> None:
    pool = await get_pool()
    async with pool.acquire() as conn:
        # Find all partners with the placeholder name
        partners = await conn.fetch(
            "SELECT id, name, owner_manager_id FROM partners WHERE name = 'Partner Name'"
        )
        if not partners:
            print("No partners with name='Partner Name' found.")
            return

        print(f"Found {len(partners)} partner(s) with placeholder name:\n")

        for p in partners:
            pid = p["id"]
            print(f"  Partner id: {pid}")

            # Look at their chats
            chats = await conn.fetch(
                "SELECT chat_id, title FROM chats WHERE partner_id = $1", pid
            )
            if not chats:
                print("    No chats linked — cannot auto-fix. Fix manually.\n")
                continue

            for chat in chats:
                title = chat["title"] or ""
                print(f"    Chat {chat['chat_id']}: title = {title!r}")
                m = _TITLE_RE.match(title)
                if m:
                    new_name = m.group(2).strip()
                    print(f"    → extracted name: {new_name!r}")
                    if not DRY_RUN:
                        # Check if a partner with that name already exists
                        existing = await conn.fetchrow(
                            "SELECT id FROM partners WHERE name = $1", new_name
                        )
                        if existing and existing["id"] != pid:
                            print(
                                f"    ⚠ Partner '{new_name}' already exists "
                                f"(id={existing['id']}). Skipping to avoid UNIQUE conflict."
                            )
                        else:
                            await conn.execute(
                                "UPDATE partners SET name = $1 WHERE id = $2",
                                new_name,
                                pid,
                            )
                            print(f"    ✓ Updated partners.name → '{new_name}'")
                    else:
                        print(f"    (dry-run: would set name → '{new_name}')")
                else:
                    print(
                        "    Title doesn't match expected format. "
                        "Fix manually with: "
                        f"UPDATE partners SET name='<correct_name>' WHERE id='{pid}';"
                    )
            print()

    await close_pool()


if __name__ == "__main__":
    asyncio.run(_run())
