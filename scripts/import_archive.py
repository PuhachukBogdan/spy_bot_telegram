"""Import partner-chat history from a Telegram Desktop HTML export archive.

Dry run by default: prints, per aff_id folder, where its history would go, how many
messages would be inserted, and every ambiguity found — and writes nothing.

    python scripts/import_archive.py                      # full dry-run report
    python scripts/import_archive.py --roster             # who is staff, and why
    python scripts/import_archive.py --aff 58329          # one folder in detail
    python scripts/import_archive.py --apply              # import the history
    python scripts/import_archive.py --apply --media      # …and upload the media
    python scripts/import_archive.py --media              # media only (resumable)

``--apply`` requires migration 0023: without ``chats.source`` /
``chats.import_aff_id`` the archive units cannot be created, and — more importantly
— without the retention change ``purge_old_data()`` would delete the whole import
on the next Sunday run. The script refuses to write until the migration is present.

Both phases are idempotent, so a run interrupted halfway can simply be repeated.
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from src.db.client import acquire_connection, close_pool  # noqa: E402
from src.importer.apply import apply_all  # noqa: E402
from src.importer.load import (  # noqa: E402
    has_import_columns,
    load_db_chats,
    load_existing_message_ids,
)
from src.importer.matcher import MatchPlan, build_plan, summarise  # noqa: E402
from src.importer.media import upload_archive_media  # noqa: E402
from src.importer.parser import parse_archive  # noqa: E402
from src.importer.roster import apply_roster, build_roster  # noqa: E402

DEFAULT_ROOT = Path(__file__).resolve().parent.parent / "AFFS_CHATS"


def _print_roster(roster: dict[str, object]) -> None:
    from src.importer.roster import RosterEntry

    entries = [e for e in roster.values() if isinstance(e, RosterEntry)]
    for role in ("internal", "anonymous_admin"):
        group = sorted(
            (e for e in entries if e.role == role), key=lambda e: -e.message_count
        )
        print(f"\n=== {role} ({len(group)}) ===")
        for entry in group:
            print(f"  {entry.message_count:>6} msgs  {entry.chat_count:>4} chats  {entry.name}")
            print(f"         {'; '.join(entry.reasons)}")
    partners = [e for e in entries if e.role == "partner"]
    print(f"\n=== partner ({len(partners)}) === (top 15 by volume)")
    for entry in sorted(partners, key=lambda e: -e.message_count)[:15]:
        print(f"  {entry.message_count:>6} msgs  {entry.chat_count:>4} chats  {entry.name}")


def _print_plan(plans: list[MatchPlan]) -> None:
    ambiguous = [p for p in plans if p.ambiguous]
    duplicates = [p for p in plans if p.action == "skip-duplicate"]
    creating = [p for p in plans if p.action == "create-archived"]
    attaching = [p for p in plans if p.action == "attach"]
    # Skipped folders are reported under their own reason; listing an empty one here
    # too would double-count it.
    empty = [p for p in plans if not p.export.messages and not p.action.startswith("skip-")]

    print(f"\n=== ATTACH to an existing chat ({len(attaching)}) ===")
    print(f"{'aff':<8}{'new':>7}{'dup':>6}  {'status':<10}{'wm':<5} chat")
    for plan in sorted(attaching, key=lambda p: -p.new_messages):
        target = plan.target
        assert target is not None
        watermark = "none" if target.last_processed_at is None else "set"
        print(f"{plan.export.aff_id:<8}{plan.new_messages:>7}{plan.colliding:>6}  "
              f"{target.status:<10}{watermark:<5} {str(target.chat_name)[:52]}")

    print(f"\n=== CREATE archived unit ({len(creating)}) ===")
    print(f"{'aff':<8}{'msgs':>7}  title")
    for plan in sorted(creating, key=lambda p: -p.new_messages):
        print(f"{plan.export.aff_id:<8}{plan.new_messages:>7}  "
              f"{str(plan.export.chat_title)[:60]}")

    if duplicates:
        print(f"\n=== SKIP as duplicate ({len(duplicates)}) ===")
        for plan in duplicates:
            print(f"  {plan.export.aff_id} duplicates {plan.duplicate_of}  "
                  f"({len(plan.export.messages)} msgs)  {plan.export.chat_title}")

    excluded = [p for p in plans if p.action == "skip-excluded"]
    if excluded:
        print(f"\n=== SKIP as excluded ({len(excluded)}) ===")
        for plan in excluded:
            print(f"  {plan.export.aff_id}  ({len(plan.export.messages)} msgs)  "
                  f"{plan.export.chat_title}")
            print(f"      {plan.excluded_reason}")

    if ambiguous:
        print(f"\n=== AMBIGUOUS — matched more than one chats row ({len(ambiguous)}) ===")
        print("    (destination is the first line; review before applying)")
        for plan in ambiguous:
            print(f"  {plan.export.aff_id}  matched on {', '.join(plan.matched_on)}")
            for index, chat in enumerate(plan.candidates):
                marker = "->" if index == 0 else "  "
                print(f"     {marker} [{chat.status:<10} live_msgs={chat.message_count:<5}] "
                      f"{str(chat.chat_name)[:52]}")

    if empty:
        print(f"\n=== EMPTY folders — nothing to import ({len(empty)}) ===")
        print("  " + ", ".join(p.export.aff_id for p in empty))


def _print_detail(plan: MatchPlan) -> None:
    export = plan.export
    print(f"=== {export.aff_id} — {export.chat_title} ===")
    print(f"  action        : {plan.action}")
    print(f"  aff_ids       : {', '.join(export.aff_ids)}")
    print(f"  matched on    : {', '.join(plan.matched_on) or '—'}")
    if plan.duplicate_of:
        print(f"  duplicate of  : {plan.duplicate_of}")
    if plan.target is not None:
        target = plan.target
        print(f"  target chat   : {target.chat_name}")
        print(f"                  status={target.status} live_msgs={target.message_count} "
              f"watermark={target.last_processed_at}")
    for other in plan.candidates[1:]:
        print(f"  ALSO MATCHED  : {other.chat_name}  (status={other.status}, "
              f"live_msgs={other.message_count})")
    print(f"  messages      : {len(export.messages)} parsed → "
          f"{plan.new_messages} new, {plan.colliding} already present")
    print(f"  events        : {len(export.events)}")
    print(f"  span          : {export.first_timestamp} .. {export.last_timestamp}")
    media = sum(len(m.media_paths) for m in export.messages)
    omitted = sum(1 for m in export.messages if m.media_omitted)
    print(f"  media on disk : {media}   referenced-but-absent: {omitted}")
    print(f"  source files  : {', '.join(export.source_files)}")
    print("\n  first 5 messages:")
    for message in export.messages[:5]:
        text = (message.text or f"[{message.message_type}]").replace("\n", " ")[:80]
        print(f"    {message.timestamp:%Y-%m-%d %H:%M} [{message.sender_role:<15}] "
              f"{str(message.sender_name)[:20]:<20} {text}")


async def _run(
    root: Path,
    aff: str | None,
    show_roster: bool,
    apply: bool,
    media: bool,
) -> None:
    print(f"parsing {root} …")
    exports = parse_archive(root)
    roster = build_roster(exports)
    exports = apply_roster(exports, roster)
    print(f"parsed {len(exports)} folders, "
          f"{sum(len(e.messages) for e in exports)} messages")

    if show_roster:
        _print_roster(dict(roster))
        return

    try:
        async with acquire_connection() as conn:
            migrated = await has_import_columns(conn)
            db_chats = await load_db_chats(conn)
            plans = build_plan(exports, db_chats)
            targets = [p.target.id for p in plans if p.target is not None]
            existing = await load_existing_message_ids(conn, targets)
            plans = build_plan(exports, db_chats, existing)

        if aff is not None:
            match = next((p for p in plans if p.export.aff_id == aff), None)
            if match is None:
                sys.exit(f"no such aff folder in the archive: {aff}")
            _print_detail(match)
            return

        if not apply and not media:
            _print_plan(plans)
            totals = summarise(plans)
            print("\n=== totals ===")
            for key, value in totals.items():
                print(f"  {key:<20}{value:>8}")
            print("\nDRY RUN — nothing written. Add --apply to import.")
            return

        if not migrated:
            sys.exit(
                "migration 0023 is NOT applied — refusing to write.\n"
                "  chats.source / chats.import_aff_id are missing, so archive units "
                "cannot be created,\n"
                "  and purge_old_data() would delete the whole import on the next "
                "Sunday 03:00 UTC run.\n"
                "  Apply supabase/migrations/0023_archive_import.sql first."
            )

        if apply:
            print(f"\n=== importing {len(plans)} folders ===")
            result = await apply_all(acquire_connection, plans)
            print(f"  chats created   : {result.chats_created}")
            print(f"  chats reused    : {result.chats_reused}")
            print(f"  messages added  : {result.messages_inserted}")
            print(f"  messages skipped: {result.messages_skipped} (already present)")
            print(f"  events added    : {result.events_inserted}")
            print(f"  folders skipped : {result.folders_skipped}")
            if result.errors:
                print(f"  ERRORS ({len(result.errors)}):")
                for error in result.errors[:20]:
                    print(f"    {error}")

        if media:
            print("\n=== uploading media to Supabase Storage ===")
            media_result = await upload_archive_media(acquire_connection, root)
            print(f"  uploaded  : {media_result.uploaded} "
                  f"({media_result.bytes_sent / 1024 / 1024:.1f} MB)")
            print(f"  missing   : {media_result.missing}")
            print(f"  too large : {media_result.too_large}")
            print(f"  failed    : {media_result.failed}")
            for error in media_result.errors[:20]:
                print(f"    {error}")
    finally:
        await close_pool()


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", type=Path, default=DEFAULT_ROOT)
    parser.add_argument("--aff", help="show one folder in detail")
    parser.add_argument("--roster", action="store_true", help="show the staff roster")
    parser.add_argument("--apply", action="store_true", help="write the history to the DB")
    parser.add_argument(
        "--media", action="store_true", help="upload photos/documents to Supabase Storage"
    )
    args = parser.parse_args()

    # Validated here rather than inside the coroutine: filesystem calls in async
    # context trip ASYNC240, and failing before the event loop starts is clearer.
    if not args.root.is_dir():
        sys.exit(f"archive root not found: {args.root}")

    import asyncio

    asyncio.run(_run(args.root, args.aff, args.roster, args.apply, args.media))
