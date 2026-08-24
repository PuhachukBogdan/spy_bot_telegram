"""Deciding where each archive folder's history goes.

Pure functions over already-loaded data: the caller fetches ``chats`` rows and the
existing ``telegram_message_id`` sets, so the whole plan is testable without a DB
and printable as a dry run before anything is written.

The join key is the affiliate id. It appears in the export folder name and, usually,
in the Telegram chat title on both sides (``CGS | ID 59743 | BETONWIN``), so titles
match literally. Three complications are handled explicitly rather than guessed at:

* **One chat, several aff_ids.** 21 titles carry more than one
  (``LEGENDS | Betonwin | 58329 | 71862 | 74849``), so an export can match a chat
  through an id that is not its folder name.
* **Several DB rows, one chat.** Re-onboarding left duplicates — ``76812`` exists as
  both ``active`` and ``pending``; ``80958`` and ``81405`` as ``active`` and
  ``abandoned``. Picking the wrong row would strand the history in a unit no report
  reads, so rows are ranked by status and then by how much live traffic they hold.
* **One chat, two folders.** ``77106`` and ``78284`` are byte-identical exports of
  the same conversation. Importing both would double every message, so duplicates
  are collapsed onto the folder whose id the chat title leads with.

Ambiguity is never resolved silently: every candidate is kept on the plan so the dry
run can show it, because a 4-digit id like ``1701`` in ``BETONWIN 1701`` is short
enough to collide with an unrelated chat's aff_id.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import datetime
from uuid import UUID

from src.importer.parser import ParsedExport

#: 4–6 digit runs, matching the observed aff_id space (1017 … 89876).
_RE_ID = re.compile(r"\b(\d{4,6})\b")

#: Preference when several ``chats`` rows match one export. ``active`` is where live
#: traffic lands; ``pending``/``abandoned`` duplicates are leftovers.
_STATUS_RANK = {"active": 0, "pending": 1, "inactive": 2, "abandoned": 3, "banned": 4}

#: Export folders to skip, named individually after review.
#:
#: Deliberately NOT a "fewer than N messages" rule. Message count does not tell you
#: whether a history is junk: plenty of legitimately quiet partner chats hold only a
#: handful of messages, and a size threshold would drop those silently while these
#: two would still need naming anyway. An explicit list states what was excluded and
#: why, and shows up in the dry-run report instead of vanishing into a cutoff.
EXCLUDED_AFF_IDS: dict[str, str] = {
    "66570": "reviewed 2026-08-05 — 2-message stub, no usable history",
    "80511": "reviewed 2026-08-05 — 17-message stub, no usable history",
}


@dataclass(frozen=True)
class DbChat:
    """The subset of a ``chats`` row the matcher needs."""

    id: UUID
    telegram_chat_id: int
    chat_name: str | None
    status: str
    unit_type: str
    message_count: int
    last_processed_at: datetime | None
    source: str = "live"
    import_aff_id: str | None = None

    @property
    def ids(self) -> frozenset[str]:
        return frozenset(_RE_ID.findall(self.chat_name or ""))


@dataclass(frozen=True)
class MatchPlan:
    """What the importer intends to do with one export folder."""

    export: ParsedExport
    #: Chosen destination, or None when an ``archived`` unit must be created.
    target: DbChat | None
    #: Every candidate row, best first — kept so the dry run can show ambiguity.
    candidates: tuple[DbChat, ...]
    #: aff_ids that produced the match.
    matched_on: tuple[str, ...]
    #: Set when this export duplicates another folder; it is then skipped.
    duplicate_of: str | None
    #: Set when the folder is on :data:`EXCLUDED_AFF_IDS`; carries the reason.
    excluded_reason: str | None
    #: Messages that would be inserted.
    new_messages: int
    #: Messages already present under the same (chat, telegram_message_id).
    colliding: int

    @property
    def action(self) -> str:
        if self.excluded_reason is not None:
            return "skip-excluded"
        if self.duplicate_of is not None:
            return "skip-duplicate"
        if self.target is None:
            return "create-archived"
        return "attach"

    @property
    def ambiguous(self) -> bool:
        return len(self.candidates) > 1


def _rank(chat: DbChat) -> tuple[int, int]:
    """Sort key: healthiest status first, then the row holding the most traffic."""
    return (_STATUS_RANK.get(chat.status, 9), -chat.message_count)


def _canonical_duplicates(exports: list[ParsedExport]) -> dict[str, str]:
    """Map each duplicate folder's aff_id → the folder kept for it.

    Exports whose message streams hash identically are the same conversation. The
    keeper is the folder whose id the chat title mentions first — for
    ``78284 | (77106) | MetaForge | Betonwin`` that is ``78284``, the id the chat is
    actually named after. Falls back to the lowest aff_id when the title settles
    nothing, so the choice is at least stable across runs.
    """
    by_hash: dict[str, list[ParsedExport]] = {}
    for export in exports:
        # Empty folders are not "duplicates" of each other, and an excluded folder
        # must never become a keeper: it would skip its twin as a duplicate and then
        # be dropped itself, losing the history entirely.
        if export.messages and export.aff_id not in EXCLUDED_AFF_IDS:
            by_hash.setdefault(export.content_hash, []).append(export)

    duplicates: dict[str, str] = {}
    for group in by_hash.values():
        if len(group) < 2:
            continue

        def _title_position(export: ParsedExport) -> tuple[int, str]:
            ids = _RE_ID.findall(export.chat_title or "")
            position = ids.index(export.aff_id) if export.aff_id in ids else len(ids)
            return (position, export.aff_id)

        keeper = min(group, key=_title_position)
        for export in group:
            if export.aff_id != keeper.aff_id:
                duplicates[export.aff_id] = keeper.aff_id
    return duplicates


def build_plan(
    exports: list[ParsedExport],
    db_chats: list[DbChat],
    existing_message_ids: dict[UUID, frozenset[int]] | None = None,
) -> list[MatchPlan]:
    """Resolve every export to a destination, without touching the database.

    *existing_message_ids* maps a chat id to the ``telegram_message_id`` values it
    already holds, so the plan can report how many rows would be no-ops under the
    ``UNIQUE (chat_id, telegram_message_id)`` constraint.
    """
    existing = existing_message_ids or {}

    by_id: dict[str, list[DbChat]] = {}
    for chat in db_chats:
        for aff in chat.ids:
            by_id.setdefault(aff, []).append(chat)
    # An archived unit already carries its origin explicitly.
    by_import_aff = {c.import_aff_id: c for c in db_chats if c.import_aff_id}

    duplicates = _canonical_duplicates(exports)
    plans: list[MatchPlan] = []

    for export in exports:
        matched_on: list[str] = []
        candidates: list[DbChat] = []

        previous = by_import_aff.get(export.aff_id)
        if previous is not None:
            candidates.append(previous)
            matched_on.append(export.aff_id)

        # Folder id first, then any extra ids from the title: a match on the folder
        # id is stronger evidence than one on a number that happens to appear.
        for aff in export.aff_ids:
            for chat in by_id.get(aff, ()):
                if chat not in candidates:
                    candidates.append(chat)
                    if aff not in matched_on:
                        matched_on.append(aff)

        candidates.sort(key=_rank)
        target = candidates[0] if candidates else None

        duplicate_of = duplicates.get(export.aff_id)
        excluded_reason = EXCLUDED_AFF_IDS.get(export.aff_id)
        if excluded_reason is not None or duplicate_of is not None:
            new_messages = colliding = 0
        elif target is not None:
            present = existing.get(target.id, frozenset())
            colliding = sum(1 for m in export.messages if m.telegram_message_id in present)
            new_messages = len(export.messages) - colliding
        else:
            colliding = 0
            new_messages = len(export.messages)

        plans.append(
            MatchPlan(
                export=export,
                target=target,
                candidates=tuple(candidates),
                matched_on=tuple(matched_on),
                duplicate_of=duplicate_of,
                excluded_reason=excluded_reason,
                new_messages=new_messages,
                colliding=colliding,
            )
        )

    return plans


def summarise(plans: list[MatchPlan]) -> dict[str, int]:
    """Headline counts for the dry-run report."""
    imported = [p for p in plans if not p.action.startswith("skip-")]
    return {
        "folders": len(plans),
        "attach": sum(1 for p in plans if p.action == "attach"),
        "create_archived": sum(1 for p in plans if p.action == "create-archived"),
        "skip_duplicate": sum(1 for p in plans if p.action == "skip-duplicate"),
        "skip_excluded": sum(1 for p in plans if p.action == "skip-excluded"),
        "ambiguous": sum(1 for p in plans if p.ambiguous),
        "messages_new": sum(p.new_messages for p in plans),
        "messages_colliding": sum(p.colliding for p in plans),
        "events": sum(len(p.export.events) for p in imported),
        "media_files": sum(len(m.media_paths) for p in imported for m in p.export.messages),
    }
