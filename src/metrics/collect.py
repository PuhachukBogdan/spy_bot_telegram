"""Turn raw rows into per-manager numbers. The pairing logic is pure and tested.

Ownership rule used throughout: an SLA wait belongs to the manager who **owns the
chat** (``chats.authorized_by``), not to whoever happened to answer. A partner
waiting in someone's chat is that person's responsibility even when a colleague
covers for them, and crediting the replier instead would let an unanswered chat
belong to nobody at all — the one case that most needs an owner.
"""

from __future__ import annotations

from collections.abc import Iterable, Sequence
from dataclasses import dataclass, field
from datetime import date, datetime
from typing import Any
from uuid import UUID

from src.config import settings
from src.metrics.attribution import RiskAttribution, attribute_risk
from src.metrics.sla import SlaOutcome, SlaTally, SlaThresholds, classify_response, tally
from src.metrics.workhours import EffectiveWorkHours, starts_a_timer

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

    @property
    def counts(self) -> bool:
        """Only the manager's own conduct may move their numbers (§5.4)."""
        return self.attribution.counts


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


def pair_waits_dated(
    messages: Sequence[dict[str, Any]],
    hours_by_manager: dict[UUID, EffectiveWorkHours],
    *,
    holidays: frozenset[date] = frozenset(),
    thresholds: SlaThresholds | None = None,
) -> dict[UUID, list[tuple[datetime, SlaOutcome]]]:
    """Walk each conversation once, producing (wait started, outcome) per manager.

    ``messages`` must already be ordered by ``(chat_id, timestamp)``.

    Each outcome keeps the instant its wait BEGAN — when the partner asked, not
    when the reply came. That is the timestamp trend buckets key on: a question
    asked late Tuesday and answered Wednesday morning is Tuesday's demand.

    A run of consecutive non-internal messages is **one** wait, timed from the
    first of them: a partner who sends five lines in twenty seconds has asked one
    question, and counting five would measure how talkative the partner is.

    A wait is only opened if its first message lands inside the owning manager's
    working hours (:func:`starts_a_timer`) — nights, weekends and holidays never
    become waits at all, which is what keeps the elapsed time plain wall-clock.
    """
    outcomes: dict[UUID, list[tuple[datetime, SlaOutcome]]] = {}
    current_chat: UUID | None = None
    waiting_since: datetime | None = None
    manager_id: UUID | None = None

    def close(waited: float | None, chars: int | None) -> None:
        if manager_id is not None and waiting_since is not None:
            outcomes.setdefault(manager_id, []).append(
                (waiting_since, classify_response(waited, chars, thresholds=thresholds))
            )

    for row in messages:
        chat_id: UUID = row["chat_id"]
        if chat_id != current_chat:
            # A chat ending while a wait is open = nobody ever replied in-window.
            if waiting_since is not None:
                close(None, None)
            current_chat, waiting_since, manager_id = chat_id, None, None

        owner: UUID | None = row["manager_id"]
        moment: datetime = row["timestamp"]
        is_internal = row["sender_role"] in _INTERNAL_ROLES

        if is_internal:
            if waiting_since is not None:
                close((moment - waiting_since).total_seconds(), row["chars"])
                waiting_since = None
            continue

        if waiting_since is not None or owner is None:
            continue  # already waiting (same question), or nobody to attribute to
        hours = hours_by_manager.get(owner)
        if hours is not None and starts_a_timer(moment, hours.hours, holidays=holidays):
            waiting_since, manager_id = moment, owner

    if waiting_since is not None:
        close(None, None)
    return outcomes


def pair_waits(
    messages: Sequence[dict[str, Any]],
    hours_by_manager: dict[UUID, EffectiveWorkHours],
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


def risks_by_manager(
    rows: Iterable[dict[str, Any]], manager_index: dict[int, UUID]
) -> dict[UUID, list[RiskCase]]:
    """Attach risk cases to the manager who OWNS the chat, tagged by authorship.

    Ownership, not authorship, decides whose page a case appears on: a partner
    raising a concern in someone's chat is that person's case to know about.
    Authorship decides whether it counts — :func:`attribute_risk` marks a case
    the manager did not write as context, and context never moves a number.
    """
    out: dict[UUID, list[RiskCase]] = {}
    for row in rows:
        attribution, _ = attribute_risk(row["sender_id"], manager_index)
        out.setdefault(row["manager_id"], []).append(
            RiskCase(
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
            )
        )
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
