"""Deciding who is staff and who is a partner, for imported history.

The export carries no user ids, so ``messages.sender_role`` cannot be resolved by
lookup the way live ingestion does — it has to be *inferred* from display names
plus archive-wide behaviour. Getting this right matters more than it looks: the
Tier-2 risk contract reasons in terms of "internal employee proposed X to a
partner", so a mislabelled sender changes the verdict, not just a report column.

Three independent signals, strongest first:

1. **Portfolio span.** Staff appear across the whole book; a partner contact
   appears in their own chat. The archive is sharply bimodal — seven senders sit
   in 92–173 chats, then it falls off a cliff to 5 — so a chat-count threshold
   separates them cleanly and is the only signal that catches staff whose display
   name carries no brand tag (``Christopher``: 145 chats, ``AffOps Helper``: 123).
2. **Admin actions across several chats.** Whoever invites people, pins messages
   or changes the group photo is operating the chat. Counted per *distinct chat*,
   never in absolute terms: a partner admins their own group too, so raw counts
   promote the entire partner side to staff (``Менеджер Alfaleads``, invited once,
   in one chat). Only acting this way across ``min_action_chats`` different chats
   is staff behaviour.
3. **Brand tag.** Internal accounts keep a brand tag in their Telegram name
   (``Mirror | Betonwin``, ``Сhicco| Betonwin`` — note the missing space and the
   Cyrillic homoglyph). Catches low-volume staff the span threshold misses
   (``Bohdan | Betonwin``: 5 chats, ``Vadym | Betonwin``: 2).

``internal_users.telegram_accounts`` is deliberately NOT the primary source: only
four real humans are registered there, which is why the live pipeline currently
labels ``Geralt | Betonwin`` — 173 chats, 130 pins — as a *partner*.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, replace

from src.importer.parser import ParsedExport, ParsedMessage

#: Display-name fragments marking an internal account.
BRAND_MARKERS = ("betonwin", "beton.win", "beton win", "beton-win")

#: Telegram's placeholder names for accounts it cannot resolve. Deliberately kept
#: as ``anonymous_admin`` even though "Deleted Account" spans 165 chats: the label
#: collapses an unknown number of distinct people, so claiming it is one employee
#: would be a fabrication.
ANONYMOUS_NAMES = frozenset({"deleted account", "deleted", "unknown", ""})

#: Distinct chats a sender must appear in to be treated as staff. The archive gap
#: between the 7th sender (92 chats) and the 8th (5) makes any value in 6…90
#: behave identically; 8 is chosen to sit just above the partner-side cluster.
DEFAULT_MIN_CHATS = 8

#: Distinct chats a sender must invite/pin/administrate in before that counts as
#: staff behaviour rather than a partner running their own group.
DEFAULT_MIN_ACTION_CHATS = 3

_RE_INVITED = re.compile(r"^(?P<actor>.+?) invited (?P<target>.+)$")
_RE_ADMIN_ACTION = re.compile(
    r"^(?P<actor>.+?) (?:pinned this message|changed group photo"
    r"|changed group title.*|removed group photo|joined group by link.*)$"
)


@dataclass(frozen=True)
class RosterEntry:
    """One display name and the evidence behind its assigned role."""

    name: str
    role: str
    chat_count: int
    message_count: int
    #: Human-readable evidence, for the dry-run report and for auditing a
    #: classification that a human may want to override.
    reasons: tuple[str, ...]


def _normalise(name: str) -> str:
    return name.strip().lower()


def has_brand_tag(name: str) -> bool:
    return any(marker in _normalise(name) for marker in BRAND_MARKERS)


def build_roster(
    exports: list[ParsedExport],
    *,
    db_internal_names: frozenset[str] = frozenset(),
    force_internal: frozenset[str] = frozenset(),
    force_partner: frozenset[str] = frozenset(),
    min_chats: int = DEFAULT_MIN_CHATS,
    min_action_chats: int = DEFAULT_MIN_ACTION_CHATS,
) -> dict[str, RosterEntry]:
    """Classify every display name in *exports* as internal / partner / anonymous.

    *force_internal* / *force_partner* are manual overrides keyed on the exact
    display name, applied last and recorded in ``reasons`` so an override is never
    silently indistinguishable from an inference.
    """
    chats: dict[str, set[str]] = {}
    messages: dict[str, int] = {}
    invite_chats: dict[str, set[str]] = {}
    admin_chats: dict[str, set[str]] = {}
    #: Display names that are really the group posting as itself, not a person.
    self_posting: set[str] = set()

    for export in exports:
        title = (export.chat_title or "").strip()
        for message in export.messages:
            if message.sender_name is None:
                continue
            chats.setdefault(message.sender_name, set()).add(export.aff_id)
            messages[message.sender_name] = messages.get(message.sender_name, 0) + 1
            if title and message.sender_name.strip() == title:
                self_posting.add(message.sender_name)
        for event in export.events:
            if (match := _RE_INVITED.match(event.text)) is not None:
                actor = match.group("actor").strip()
                invite_chats.setdefault(actor, set()).add(export.aff_id)
            elif (match := _RE_ADMIN_ACTION.match(event.text)) is not None:
                actor = match.group("actor").strip()
                admin_chats.setdefault(actor, set()).add(export.aff_id)

    db_normalised = {_normalise(n) for n in db_internal_names}
    roster: dict[str, RosterEntry] = {}

    # Event actors are included even when they never posted: an ops account that
    # only invites and pins would otherwise be absent from the roster entirely.
    for name in sorted(set(chats) | set(invite_chats) | set(admin_chats)):
        normalised = _normalise(name)
        chat_count = len(chats.get(name, ()))
        invited_in = len(invite_chats.get(name, ()))
        administered = len(admin_chats.get(name, ()))
        reasons: list[str] = []

        if chat_count >= min_chats:
            reasons.append(f"present in {chat_count} chats")
        if has_brand_tag(name):
            reasons.append("brand tag in display name")
        if invited_in >= min_action_chats:
            reasons.append(f"invited people in {invited_in} chats")
        if administered >= min_action_chats:
            reasons.append(f"pinned/administered {administered} chats")
        if normalised in db_normalised:
            reasons.append("listed in internal_users")

        if normalised in ANONYMOUS_NAMES:
            role = "anonymous_admin"
            reasons = ["Telegram placeholder name — collapses unknown accounts", *reasons]
        elif name in force_internal:
            role, reasons = "internal", ["manual override → internal", *reasons]
        elif name in force_partner:
            role, reasons = "partner", ["manual override → partner", *reasons]
        elif name in self_posting:
            # The chat's own title as the sender = anonymous group admin. Such a
            # name inherits the brand tag from the chat, which would otherwise
            # read as staff.
            role = "anonymous_admin"
            reasons = ["display name equals the chat title — group posting as itself"]
        elif reasons:
            role = "internal"
        else:
            role = "partner"

        roster[name] = RosterEntry(
            name=name,
            role=role,
            chat_count=chat_count,
            message_count=messages.get(name, 0),
            reasons=tuple(reasons),
        )

    return roster


def apply_roster(
    exports: list[ParsedExport], roster: dict[str, RosterEntry]
) -> list[ParsedExport]:
    """Return *exports* with every ``sender_role`` replaced by the roster verdict."""
    updated: list[ParsedExport] = []
    for export in exports:
        messages: list[ParsedMessage] = []
        for message in export.messages:
            entry = roster.get(message.sender_name or "")
            role = entry.role if entry is not None else "unknown"
            messages.append(
                message if message.sender_role == role else replace(message, sender_role=role)
            )
        updated.append(replace(export, messages=tuple(messages)))
    return updated
