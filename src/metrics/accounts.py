"""Per-Telegram-account usage for a person with more than one account. Pure.

In 2026-09 every manager got a second Telegram account (``X | Stalker``) and the
two were merged into one person (``/link_account``). The page still measures the
PERSON — SLA, coverage, tone are theirs whichever account they used — but the
user also asked to see *how the accounts are used*: is the old one still
writing, has the new one taken over. That is what these rows answer: messages,
active days, chats present, replies given and how many of those were on time,
per account, labelled ``old`` / ``new``.

Labels come from ``internal_users.account_labels`` when set, otherwise from
which account was seen first (``chat_members.first_seen_at``, which outlives
message retention). A person with one account gets no label.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime
from typing import Any
from uuid import UUID
from zoneinfo import ZoneInfo

from src.metrics.collect import WaitOutcome
from src.metrics.membership import Crews, local_day


@dataclass(frozen=True)
class AccountStats:
    """One account of one person over the detail window."""

    telegram_id: int
    label: str | None
    chats: int
    messages: int
    active_days: int
    last_active_at: datetime | None
    replies: int
    replies_on_time: int

    def to_payload(self) -> dict[str, Any]:
        return {
            "id": str(self.telegram_id),
            "label": self.label,
            "chats": self.chats,
            "messages": self.messages,
            "activeDays": self.active_days,
            "lastActiveAt": (
                self.last_active_at.isoformat(timespec="minutes")
                if self.last_active_at is not None
                else None
            ),
            "replies": self.replies,
            "repliesOnTime": self.replies_on_time,
        }


def label_accounts(
    accounts: Sequence[int],
    explicit: Mapping[str, str],
    first_seen: Mapping[int, datetime],
) -> dict[int, str | None]:
    """``old`` / ``new`` per account. Explicit labels win; the rest by first sight.

    With two or more accounts and no explicit ``old``, the account seen earliest
    is ``old`` and every other one ``new``. An account never seen anywhere sorts
    last (it cannot be the old one). A single account carries only an explicit
    label, if any.
    """
    labels: dict[int, str | None] = {a: explicit.get(str(a)) for a in accounts}
    if len(accounts) < 2:
        return labels
    unlabeled = [a for a in accounts if labels[a] is None]
    if not unlabeled:
        return labels
    has_old = "old" in labels.values()

    def sort_key(account: int) -> tuple[int, float, int]:
        seen = first_seen.get(account)
        return (
            0 if seen is not None else 1,
            seen.timestamp() if seen is not None else 0.0,
            accounts.index(account),
        )

    for position, account in enumerate(sorted(unlabeled, key=sort_key)):
        labels[account] = "new" if (has_old or position > 0) else "old"
    return labels


def account_stats(
    accounts: Sequence[int],
    *,
    labels: Mapping[int, str | None],
    sender_day_rows: Iterable[Mapping[str, Any]],
    waits: Iterable[WaitOutcome],
    crews: Crews,
    since: datetime,
    tz: ZoneInfo,
) -> list[AccountStats]:
    """Fold the horizon rows down to the detail window, per account.

    ``sender_day_rows`` are ``(sender_id, chat_id, day, messages, last_at)``;
    only rows on or after the local day of ``since`` count. Replies are the rated
    waits this account closed, from ``since`` on.
    """
    wanted = set(accounts)
    since_day = local_day(since, tz)
    messages: dict[int, int] = {}
    days: dict[int, set[Any]] = {}
    last: dict[int, datetime] = {}
    for row in sender_day_rows:
        account = row["sender_id"]
        if account not in wanted or row["day"] < since_day:
            continue
        messages[account] = messages.get(account, 0) + row["messages"]
        days.setdefault(account, set()).add(row["day"])
        seen = row.get("last_at")
        if seen is not None and (account not in last or seen > last[account]):
            last[account] = seen
    replies: dict[int, int] = {}
    on_time: dict[int, int] = {}
    for wait in waits:
        account = wait.answered_with
        if account is None or account not in wanted or wait.started_at < since:
            continue
        replies[account] = replies.get(account, 0) + 1
        if wait.outcome.is_met:
            on_time[account] = on_time.get(account, 0) + 1
    chats = {
        account: sum(1 for present in crews.present_accounts.values() if account in present)
        for account in accounts
    }
    return [
        AccountStats(
            telegram_id=account,
            label=labels.get(account),
            chats=chats[account],
            messages=messages.get(account, 0),
            active_days=len(days.get(account, ())),
            last_active_at=last.get(account),
            replies=replies.get(account, 0),
            replies_on_time=on_time.get(account, 0),
        )
        for account in accounts
    ]


def account_days_payload(
    managers: Sequence[Any],
    *,
    labels_by_manager: Mapping[UUID, Mapping[int, str | None]],
    sender_day_rows: Iterable[Mapping[str, Any]],
    waits: Iterable[WaitOutcome],
    tz: ZoneInfo,
) -> list[dict[str, Any]]:
    """Sparse per-account day maps for client-side period recomputation.

    One entry per (manager, account): ``m``, ``a``, ``label``, ``d`` messages per
    day, ``r`` rated replies per day, ``o`` on-time replies per day — days keyed
    by the wait's START (same calendar as the SLA buckets) — and ``cd``, messages
    per chat per day.
    """
    owner_of: dict[int, UUID] = {}
    for m in managers:
        for account in m.telegram_accounts:
            owner_of[account] = m.id
    entries: dict[tuple[UUID, int], dict[str, Any]] = {}

    def entry(manager: UUID, account: int) -> dict[str, Any]:
        key = (manager, account)
        found = entries.get(key)
        if found is None:
            found = {
                "m": str(manager),
                "a": str(account),
                "label": labels_by_manager.get(manager, {}).get(account),
                "d": {},
                "r": {},
                "o": {},
                # chat id -> {day: messages}: what this account wrote where —
                # the account view's chat table and the "Moving" block read it.
                "cd": {},
            }
            entries[key] = found
        return found

    for row in sender_day_rows:
        manager = owner_of.get(row["sender_id"])
        if manager is None:
            continue
        day = row["day"].isoformat()
        e = entry(manager, row["sender_id"])
        e["d"][day] = e["d"].get(day, 0) + row["messages"]
        per_chat = e["cd"].setdefault(str(row["chat_id"]), {})
        per_chat[day] = per_chat.get(day, 0) + row["messages"]
    for wait in waits:
        account = wait.answered_with
        if account is None:
            continue
        manager = owner_of.get(account)
        if manager is None:
            continue
        day = local_day(wait.started_at, tz).isoformat()
        e = entry(manager, account)
        e["r"][day] = e["r"].get(day, 0) + 1
        if wait.outcome.is_met:
            e["o"][day] = e["o"].get(day, 0) + 1
    # Every account of every manager gets a row, even a silent one — "0 messages
    # on the new account" is a reading, not a gap.
    for m in managers:
        for account in m.telegram_accounts:
            entry(m.id, account)
    return [entries[key] for key in sorted(entries, key=lambda k: (str(k[0]), k[1]))]
