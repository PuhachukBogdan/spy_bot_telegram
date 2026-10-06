"""Which managers a chat belongs to — many of them, not one. Pure, no I/O.

Until 2026-10 the Team summary tied every chat to its single owner
(``chats.authorized_by``). Measured against Telegram that was wrong for the whole
team: a typical partner group holds 3–5 of our managers, the head of department
sits in 270 of 317 groups, and a manager who left still "owned" chats his
colleagues were working. This module replaces the owner with two sets per chat:

* the **portfolio** — every roster manager PRESENT in the chat right now
  (``chat_members``). This is what "their chats" means on the page: Active
  chats counts, the dossier's chat table, the ``ms`` list in the island.
* the **crew** of a chat on a given day — present managers who actually WROTE in
  that chat within ``lookback_days`` before that day. Presence is not
  responsibility: the head is in almost every group and works a handful, so an
  unanswered partner is charged to the people who were working the chat. When
  nobody was (a brand-new group), the chat's owner stands in, if they are a
  roster manager and still active.

Deactivation (``internal_users.deactivated_at``) cuts both: from that local day
on the person is in no crew and no portfolio, while everything before stays.

Membership is known for NOW only — Telegram does not give history — so the
portfolio is applied to every bucket as it stands today. The crew, by contrast,
is dated: it follows who wrote when, which the message table does remember.
"""

from __future__ import annotations

from bisect import bisect_left, bisect_right
from collections.abc import Iterable, Mapping
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta
from typing import Any
from uuid import UUID
from zoneinfo import ZoneInfo


@dataclass(frozen=True)
class Crews:
    """Per-chat manager sets plus the dated activity needed to pick a crew."""

    #: chat -> roster managers present now (deactivated people included; the
    #: date cut happens in :meth:`portfolio` / :meth:`crew`).
    present: dict[UUID, frozenset[UUID]] = field(default_factory=dict)
    #: chat -> staff Telegram accounts present now (for the per-account split).
    present_accounts: dict[UUID, frozenset[int]] = field(default_factory=dict)
    #: (chat, manager) -> sorted local days on which the manager wrote there.
    activity: dict[tuple[UUID, UUID], list[date]] = field(default_factory=dict)
    #: chat -> owner (``authorized_by``), the fallback when nobody works a chat.
    owner: dict[UUID, UUID | None] = field(default_factory=dict)
    #: manager -> local day from which they are attributed nothing.
    deactivated: dict[UUID, date] = field(default_factory=dict)
    #: Managers the page measures at all. Anyone else never appears in a set.
    roster: frozenset[UUID] = field(default_factory=frozenset)
    lookback_days: int = 30

    # -- membership ---------------------------------------------------------
    def is_active_on(self, manager: UUID, day: date) -> bool:
        """Not deactivated as of ``day`` (the deactivation day itself is out)."""
        cut = self.deactivated.get(manager)
        return cut is None or day < cut

    def portfolio(self, manager: UUID, *, as_of: date | None = None) -> set[UUID]:
        """Chats the manager is present in — their whole portfolio on the page."""
        if as_of is not None and not self.is_active_on(manager, as_of):
            return set()
        return {chat for chat, managers in self.present.items() if manager in managers}

    def members_of(self, chat_id: UUID, *, as_of: date | None = None) -> frozenset[UUID]:
        """Roster managers present in the chat (active as of ``as_of`` if given)."""
        managers = self.present.get(chat_id, frozenset())
        if as_of is None:
            return managers
        return frozenset(m for m in managers if self.is_active_on(m, as_of))

    # -- responsibility ------------------------------------------------------
    def wrote_recently(self, chat_id: UUID, manager: UUID, day: date) -> bool:
        """Did the manager write in this chat within the lookback ending on ``day``?"""
        days = self.activity.get((chat_id, manager))
        if not days:
            return False
        low = day - timedelta(days=self.lookback_days)
        # Any activity day in [low, day]: bisect keeps this O(log n) per wait.
        return bisect_right(days, day) > bisect_left(days, low)

    def crew(self, chat_id: UUID, day: date) -> frozenset[UUID]:
        """Who answers for this chat on ``day``.

        Present AND recently active managers; failing that, the owner if they
        are measured and active; failing that, nobody (the wait still counts
        for the team).
        """
        working = frozenset(
            m
            for m in self.members_of(chat_id, as_of=day)
            if self.wrote_recently(chat_id, m, day)
        )
        if working:
            return working
        owner = self.owner.get(chat_id)
        if owner is not None and owner in self.roster and self.is_active_on(owner, day):
            return frozenset({owner})
        return frozenset()


def local_day(moment: datetime, tz: ZoneInfo) -> date:
    return moment.astimezone(tz).date()


def build_crews(
    membership_rows: Iterable[Mapping[str, Any]],
    sender_day_rows: Iterable[Mapping[str, Any]],
    registry_rows: Iterable[Mapping[str, Any]],
    *,
    manager_index: Mapping[int, UUID],
    roster: Iterable[UUID],
    deactivated: Mapping[UUID, date],
    lookback_days: int,
) -> Crews:
    """Assemble :class:`Crews` from the three row sets the page already loads.

    ``membership_rows``: ``chat_members`` presence (``chat_id``,
    ``telegram_user_id``). ``sender_day_rows``: ``(sender_id, chat_id, day)``
    message counts for staff accounts. ``registry_rows``: every active chat with
    its ``owner_id``. Accounts are folded onto people through ``manager_index``;
    an account that maps to nobody on the roster is ignored.
    """
    roster_set = frozenset(roster)
    present: dict[UUID, set[UUID]] = {}
    present_accounts: dict[UUID, set[int]] = {}
    for row in membership_rows:
        account = row["telegram_user_id"]
        manager = manager_index.get(account)
        if manager is None or manager not in roster_set:
            continue
        present.setdefault(row["chat_id"], set()).add(manager)
        present_accounts.setdefault(row["chat_id"], set()).add(account)

    activity: dict[tuple[UUID, UUID], set[date]] = {}
    for row in sender_day_rows:
        manager = manager_index.get(row["sender_id"])
        if manager is None or manager not in roster_set:
            continue
        activity.setdefault((row["chat_id"], manager), set()).add(row["day"])

    return Crews(
        present={chat: frozenset(ms) for chat, ms in present.items()},
        present_accounts={chat: frozenset(a) for chat, a in present_accounts.items()},
        activity={key: sorted(days) for key, days in activity.items()},
        owner={row["chat_id"]: row.get("owner_id") for row in registry_rows},
        deactivated=dict(deactivated),
        roster=roster_set,
        lookback_days=lookback_days,
    )


def fan_out_by_portfolio(
    rows: Iterable[Mapping[str, Any]], crews: Crews
) -> list[dict[str, Any]]:
    """Copy each chat-keyed row once per present manager, setting ``manager_id``.

    What turns a per-chat fact (message count, creation day) into per-manager
    input for the folds that still think in ``manager_id`` — one chat, several
    people. Rows for chats with no present manager are dropped; they still reach
    the TEAM through the un-fanned original list.
    """
    out: list[dict[str, Any]] = []
    for row in rows:
        for manager in crews.members_of(row["chat_id"]):
            out.append({**row, "manager_id": manager})
    return out
