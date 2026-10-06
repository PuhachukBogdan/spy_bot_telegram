"""Turn raw rows into per-manager numbers. The pairing logic is pure and tested.

Attribution rule (2026-10, replaces "the owner of the chat"):

* A wait somebody ANSWERED in time belongs to the person who answered — met or
  missed by their own timing. A reply by someone the page does not measure (an
  admin, the CEO) closes the wait for everyone and is credited to nobody; the
  team still counts it.
* A wait nobody answered inside the offline window is charged to the chat's
  **crew** on duty — the managers present in the chat who had been working it
  (see :mod:`src.metrics.membership`) and whose working hours covered the
  moment. Absence is shared by the people who were supposed to be there.
* A chat with no crew falls back to its owner, and with no measurable owner the
  wait counts for the team only.

The previous rule — everything to ``chats.authorized_by`` — assumed one manager
per chat. Real groups hold three to five, so the owner was credited with
colleagues' replies and a departed owner kept "working" for weeks.

When no crews are supplied the old owner rule is used unchanged; that path is
kept for callers and tests that reason about a single owner.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import date, datetime
from typing import Any
from uuid import UUID
from zoneinfo import ZoneInfo

from src.config import settings
from src.metrics.attribution import RiskAttribution, attribute_risk
from src.metrics.membership import Crews, local_day
from src.metrics.sla import SlaOutcome, SlaTally, SlaThresholds, classify_response, tally
from src.metrics.workhours import EffectiveWorkHours, starts_a_timer
from src.utils.workhours import WorkHours

#: Roles that are "us". Anything else opens a wait that someone must answer.
_INTERNAL_ROLES = frozenset({"internal"})


@dataclass(frozen=True)
class ChatCoverage:
    """Active-vs-total chats for one manager."""

    total: int = 0
    active: int = 0

    @property
    def percent(self) -> float | None:
        """Share of the portfolio that saw real traffic, or ``None`` if no chats."""
        if self.total == 0:
            return None
        return round(100 * self.active / self.total, 1)


@dataclass(frozen=True)
class ChatRow:
    """One chat in a manager's portfolio, as shown in their dossier."""

    chat_id: UUID
    name: str
    unit_type: str
    messages: int
    active: bool


@dataclass(frozen=True)
class RiskCase:
    """A risk event surfaced on a manager's page, with how it got there."""

    risk_id: UUID
    chat_id: UUID
    chat_name: str
    unit_type: str
    risk_type: str
    risk_level: str
    score: int
    detected_at: datetime
    phrase: str | None
    why: str | None
    attribution: RiskAttribution
    #: Telegram account that wrote the anchor message (the account view's filter).
    sender_id: int | None = None

    @property
    def counts(self) -> bool:
        """Only the manager's own conduct may move their numbers (§5.4)."""
        return self.attribution.counts


@dataclass(frozen=True)
class WaitOutcome:
    """One partner wait, classified, with everyone it is attributed to."""

    started_at: datetime
    outcome: SlaOutcome
    chat_id: UUID
    #: The measured manager whose reply closed a RATED wait; ``None`` when the
    #: wait went offline, or the closer is not someone the page measures.
    answered_by: UUID | None = None
    #: The Telegram account that replied (any staff account), for the per-account
    #: split. ``None`` when nobody replied inside the window.
    answered_with: int | None = None
    #: Managers an unanswered wait is charged to (the crew on duty).
    charged: frozenset[UUID] = field(default_factory=frozenset)

    @property
    def managers(self) -> frozenset[UUID]:
        """Everyone whose numbers this wait moves."""
        if self.answered_by is not None:
            return frozenset({self.answered_by})
        return self.charged


@dataclass
class ManagerMetrics:
    """Everything Phase 2 currently measures for one manager."""

    manager_id: UUID
    name: str
    coverage: ChatCoverage = field(default_factory=ChatCoverage)
    sla: SlaTally = field(default_factory=SlaTally)
    proposals: int = 0
    work_hours: EffectiveWorkHours | None = None
    chats: list[ChatRow] = field(default_factory=list)
    risks: list[RiskCase] = field(default_factory=list)


def pair_waits_all(
    messages: Sequence[dict[str, Any]],
    hours_by_manager: Mapping[UUID, EffectiveWorkHours],
    *,
    crews: Crews | None = None,
    manager_index: Mapping[int, UUID] | None = None,
    tz: ZoneInfo | None = None,
    default_hours: WorkHours | None = None,
    hidden_accounts: frozenset[int] = frozenset(),
    holidays: frozenset[date] = frozenset(),
    thresholds: SlaThresholds | None = None,
) -> list[WaitOutcome]:
    """Walk each conversation once and classify every partner wait.

    ``messages`` must already be ordered by ``(chat_id, timestamp)``.

    Each outcome keeps the instant its wait BEGAN — when the partner asked, not
    when the reply came. That is the timestamp trend buckets key on: a question
    asked late Tuesday and answered Wednesday morning is Tuesday's demand.

    A run of consecutive non-internal messages is **one** wait, timed from the
    first of them: a partner who sends five lines in twenty seconds has asked one
    question, and counting five would measure how talkative the partner is.

    A wait is only opened inside somebody's working hours (:func:`starts_a_timer`)
    — nights, weekends and holidays never become waits at all, which is what
    keeps the elapsed time plain wall-clock. With crews, "somebody" is any crew
    member on duty (they are the ones charged if nobody answers); a crew-less
    chat uses ``default_hours`` and its waits count for the team only.

    ``hidden_accounts`` (a head reading their own team page): a reply from one
    of these accounts still ends the wait for everyone, but the wait itself is
    dropped — it must reach neither a colleague's numbers nor the team's.
    """
    outcomes: list[WaitOutcome] = []
    current_chat: UUID | None = None
    waiting_since: datetime | None = None
    on_duty: frozenset[UUID] = frozenset()
    owner_mode = crews is None

    def close(
        chat_id: UUID, waited: float | None, chars: int | None, closer: int | None
    ) -> None:
        if waiting_since is None:
            return
        outcome = classify_response(waited, chars, thresholds=thresholds)
        if closer is not None and closer in hidden_accounts:
            return
        if owner_mode:
            # Legacy: the owner takes every outcome, rated or offline.
            owner = next(iter(on_duty))
            outcomes.append(
                WaitOutcome(
                    started_at=waiting_since,
                    outcome=outcome,
                    chat_id=chat_id,
                    answered_by=owner if outcome.in_ratio else None,
                    answered_with=closer if outcome.in_ratio else None,
                    charged=frozenset() if outcome.in_ratio else on_duty,
                )
            )
            return
        if outcome.in_ratio:
            answered_by = (manager_index or {}).get(closer) if closer is not None else None
            outcomes.append(
                WaitOutcome(
                    started_at=waiting_since,
                    outcome=outcome,
                    chat_id=chat_id,
                    answered_by=answered_by,
                    answered_with=closer,
                )
            )
        else:
            outcomes.append(
                WaitOutcome(
                    started_at=waiting_since,
                    outcome=outcome,
                    chat_id=chat_id,
                    charged=on_duty,
                )
            )

    for row in messages:
        chat_id: UUID = row["chat_id"]
        if chat_id != current_chat:
            # A chat ending while a wait is open = nobody ever replied in-window.
            if waiting_since is not None and current_chat is not None:
                close(current_chat, None, None, None)
            current_chat, waiting_since, on_duty = chat_id, None, frozenset()

        moment: datetime = row["timestamp"]
        is_internal = row["sender_role"] in _INTERNAL_ROLES

        if is_internal:
            if waiting_since is not None:
                close(
                    chat_id,
                    (moment - waiting_since).total_seconds(),
                    row["chars"],
                    row.get("sender_id"),
                )
                waiting_since = None
            continue

        if waiting_since is not None:
            continue  # already waiting — the same question, not a new one

        if owner_mode:
            owner: UUID | None = row.get("owner_id", row.get("manager_id"))
            if owner is None:
                continue  # nobody to attribute to
            hours = hours_by_manager.get(owner)
            if hours is not None and starts_a_timer(moment, hours.hours, holidays=holidays):
                waiting_since, on_duty = moment, frozenset({owner})
            continue

        assert crews is not None
        day = local_day(moment, tz) if tz is not None else moment.date()
        crew = crews.crew(chat_id, day)
        if crew:
            duty = frozenset(
                m
                for m in crew
                if m in hours_by_manager
                and starts_a_timer(moment, hours_by_manager[m].hours, holidays=holidays)
            )
            if duty:
                waiting_since, on_duty = moment, duty
            continue
        if default_hours is not None and starts_a_timer(moment, default_hours, holidays=holidays):
            waiting_since, on_duty = moment, frozenset()

    if waiting_since is not None and current_chat is not None:
        close(current_chat, None, None, None)
    return outcomes


def by_manager(waits: Iterable[WaitOutcome]) -> dict[UUID, list[tuple[datetime, SlaOutcome]]]:
    """Fan the classified waits out to the managers they move — the trend input."""
    out: dict[UUID, list[tuple[datetime, SlaOutcome]]] = {}
    for wait in waits:
        for manager in wait.managers:
            out.setdefault(manager, []).append((wait.started_at, wait.outcome))
    return out


def pair_waits_dated(
    messages: Sequence[dict[str, Any]],
    hours_by_manager: Mapping[UUID, EffectiveWorkHours],
    *,
    holidays: frozenset[date] = frozenset(),
    thresholds: SlaThresholds | None = None,
) -> dict[UUID, list[tuple[datetime, SlaOutcome]]]:
    """Owner-rule pairing, per manager with dates (kept for single-owner callers)."""
    return by_manager(
        pair_waits_all(messages, hours_by_manager, holidays=holidays, thresholds=thresholds)
    )


def pair_waits(
    messages: Sequence[dict[str, Any]],
    hours_by_manager: Mapping[UUID, EffectiveWorkHours],
    *,
    holidays: frozenset[date] = frozenset(),
    thresholds: SlaThresholds | None = None,
) -> dict[UUID, list[SlaOutcome]]:
    """:func:`pair_waits_dated` with the dates stripped, for plain tallies."""
    dated = pair_waits_dated(
        messages, hours_by_manager, holidays=holidays, thresholds=thresholds
    )
    return {
        manager: [outcome for _, outcome in pairs] for manager, pairs in dated.items()
    }


def coverage_by_manager(
    rows: Iterable[dict[str, Any]], *, min_messages: int | None = None
) -> dict[UUID, ChatCoverage]:
    """Fold per-chat message counts into active-vs-total per manager."""
    threshold = (
        settings.ACTIVE_CHAT_MIN_MESSAGES if min_messages is None else min_messages
    )
    totals: dict[UUID, int] = {}
    actives: dict[UUID, int] = {}
    for row in rows:
        manager_id: UUID = row["manager_id"]
        totals[manager_id] = totals.get(manager_id, 0) + 1
        if row["messages"] >= threshold:
            actives[manager_id] = actives.get(manager_id, 0) + 1
    return {
        manager_id: ChatCoverage(total=total, active=actives.get(manager_id, 0))
        for manager_id, total in totals.items()
    }


def chat_label(row: dict[str, Any]) -> str:
    """Display name for a chat unit, including its forum topic when it has one."""
    name = (row.get("chat_name") or "").strip() or "—"
    topic = (row.get("topic_name") or "").strip()
    return f"{name} · {topic}" if topic else name


def chats_by_manager(
    rows: Iterable[dict[str, Any]], *, min_messages: int | None = None
) -> dict[UUID, list[ChatRow]]:
    """Per-manager chat lists, busiest first."""
    threshold = (
        settings.ACTIVE_CHAT_MIN_MESSAGES if min_messages is None else min_messages
    )
    out: dict[UUID, list[ChatRow]] = {}
    for row in rows:
        out.setdefault(row["manager_id"], []).append(
            ChatRow(
                chat_id=row["chat_id"],
                name=chat_label(row),
                unit_type=row["unit_type"],
                messages=row["messages"],
                active=row["messages"] >= threshold,
            )
        )
    for chats in out.values():
        chats.sort(key=lambda c: (-c.messages, c.name))
    return out


def _case(row: dict[str, Any], attribution: RiskAttribution) -> RiskCase:
    return RiskCase(
        risk_id=row["id"],
        chat_id=row["chat_id"],
        chat_name=chat_label(row),
        unit_type=row["unit_type"],
        risk_type=row["risk_type"],
        risk_level=row["risk_level"],
        score=row["final_score"],
        detected_at=row["created_at"],
        phrase=row["detected_phrase"],
        why=row["llm_explanation"],
        attribution=attribution,
        sender_id=row.get("sender_id"),
    )


def risks_by_manager(
    rows: Iterable[dict[str, Any]],
    manager_index: Mapping[int, UUID],
    *,
    crews: Crews | None = None,
    tz: ZoneInfo | None = None,
) -> dict[UUID, list[RiskCase]]:
    """Put each risk case on the right pages.

    With crews: a case a measured manager WROTE goes on that manager's page and
    counts; a case anyone else raised goes, as context, on the page of every
    crew member of that chat that day (the people working it) — never counted.
    A colleague's case therefore counts against the colleague and nobody else;
    the old owner rule charged it to the chat owner.

    Without crews (legacy): the case goes on the chat OWNER's page, counted only
    if the owner wrote it.
    """
    out: dict[UUID, list[RiskCase]] = {}
    for row in rows:
        attribution, author = attribute_risk(row["sender_id"], manager_index)
        if crews is None:
            owner: UUID | None = row.get("owner_id", row.get("manager_id"))
            if owner is None:
                continue
            counts = attribution.counts and author == owner
            kind = RiskAttribution.MANAGER_ACTION if counts else RiskAttribution.CHAT_CONTEXT
            out.setdefault(owner, []).append(_case(row, kind))
            continue
        if attribution.counts and author is not None and author in crews.roster:
            out.setdefault(author, []).append(_case(row, RiskAttribution.MANAGER_ACTION))
            continue
        moment: datetime = row["created_at"]
        day = local_day(moment, tz) if tz is not None else moment.date()
        for manager in crews.crew(row["chat_id"], day):
            if manager == author:
                continue
            out.setdefault(manager, []).append(_case(row, RiskAttribution.CHAT_CONTEXT))
    return out


def assemble(
    managers: Sequence[Any],
    *,
    coverage: dict[UUID, ChatCoverage],
    sla_outcomes: dict[UUID, list[SlaOutcome]],
    proposals: dict[UUID, int],
    hours: dict[UUID, EffectiveWorkHours],
    chats: dict[UUID, list[ChatRow]] | None = None,
    risks: dict[UUID, list[RiskCase]] | None = None,
) -> list[ManagerMetrics]:
    """Join the parts into one row per manager, including managers with no data.

    Managers with an empty portfolio still appear: absence of activity is itself
    a reading, and dropping them would quietly shorten the roster.
    """
    return [
        ManagerMetrics(
            manager_id=m.id,
            name=m.full_name,
            coverage=coverage.get(m.id, ChatCoverage()),
            sla=tally(sla_outcomes.get(m.id, [])),
            proposals=proposals.get(m.id, 0),
            work_hours=hours.get(m.id),
            chats=(chats or {}).get(m.id, []),
            risks=(risks or {}).get(m.id, []),
        )
        for m in managers
    ]
