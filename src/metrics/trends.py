"""Fold day-level counters into week / month / quarter trend buckets. Pure.

Two rules carried through every function here:

* **Counters fold, percentages don't.** A bucket's SLA % is computed from the
  summed met/rated counts inside it, never by averaging smaller percentages —
  and the team series sums the managers' counters, never their percentages.
* **Comparisons are to-date against to-date.** Mid-bucket, the only honest base
  is the same number of elapsed days at the start of the previous bucket:
  August-through-the-15th against July-through-the-15th. Comparing a half-lived
  bucket against a whole one manufactures a decline that isn't there.

Buckets and days live in the report timezone (Europe/Kyiv), like every other
reporting surface. Weeks start Monday; months and quarters are calendar.
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta
from enum import StrEnum
from typing import Any
from uuid import UUID
from zoneinfo import ZoneInfo

from src.config import settings
from src.metrics.attribution import attribute_risk
from src.metrics.sla import SlaOutcome


class Granularity(StrEnum):
    DAY = "day"
    WEEK = "week"
    MONTH = "month"
    QUARTER = "quarter"


def bucket_start(day: date, granularity: Granularity) -> date:
    """The first day of the bucket containing ``day``."""
    if granularity is Granularity.DAY:
        return day
    if granularity is Granularity.WEEK:
        return day - timedelta(days=day.weekday())  # Monday
    if granularity is Granularity.MONTH:
        return day.replace(day=1)
    quarter_month = ((day.month - 1) // 3) * 3 + 1
    return day.replace(month=quarter_month, day=1)


def next_bucket_start(start: date, granularity: Granularity) -> date:
    """The first day of the bucket after the one starting at ``start``."""
    if granularity is Granularity.DAY:
        return start + timedelta(days=1)
    if granularity is Granularity.WEEK:
        return start + timedelta(days=7)
    if granularity is Granularity.MONTH:
        return (
            start.replace(year=start.year + 1, month=1)
            if start.month == 12
            else start.replace(month=start.month + 1)
        )
    if start.month >= 10:
        return start.replace(year=start.year + 1, month=1)
    return start.replace(month=start.month + 3)


def prev_bucket_start(start: date, granularity: Granularity) -> date:
    """The first day of the bucket before the one starting at ``start``."""
    return bucket_start(start - timedelta(days=1), granularity)


@dataclass
class DayCounters:
    """Additive counters for one scope on one local day. Only counts — no rates."""

    sla_met: int = 0
    sla_rated: int = 0
    offline: int = 0
    proposals: int = 0
    risks_own: int = 0

    def add(self, other: DayCounters) -> None:
        self.sla_met += other.sla_met
        self.sla_rated += other.sla_rated
        self.offline += other.offline
        self.proposals += other.proposals
        self.risks_own += other.risks_own


@dataclass(frozen=True)
class ScopeDays:
    """Everything a scope (team, or one manager) contributes, keyed by local day."""

    counters: dict[date, DayCounters] = field(default_factory=dict)
    #: chat_id -> {day: messages} — coverage needs per-chat resolution per bucket.
    chat_messages: dict[UUID, dict[date, int]] = field(default_factory=dict)
    #: chat_id -> local date the chat was created (bounds the denominator).
    chat_created: dict[UUID, date] = field(default_factory=dict)

    def day_counters(self, day: date) -> DayCounters:
        found = self.counters.get(day)
        if found is None:
            found = DayCounters()
            self.counters[day] = found
        return found


@dataclass(frozen=True)
class TrendPoint:
    """One bucket (or one to-date window) of a scope's trend."""

    start: date
    end: date  # exclusive
    partial: bool
    truncated: bool
    #: Falls inside the bot's onboarding/test fortnight — drawn muted, labelled.
    test: bool
    sla_met: int
    sla_rated: int
    offline: int
    proposals: int
    risks_own: int
    coverage_active: int
    coverage_total: int

    @property
    def sla_percent(self) -> float | None:
        if self.sla_rated == 0:
            return None
        return round(100 * self.sla_met / self.sla_rated, 1)

    @property
    def coverage_percent(self) -> float | None:
        if self.coverage_total == 0:
            return None
        return round(100 * self.coverage_active / self.coverage_total, 1)

    def to_payload(self) -> dict[str, Any]:
        return {
            "start": self.start.isoformat(),
            "end": self.end.isoformat(),
            "partial": self.partial,
            "truncated": self.truncated,
            "test": self.test,
            "slaPercent": self.sla_percent,
            "slaMet": self.sla_met,
            "slaRated": self.sla_rated,
            "offline": self.offline,
            "proposals": self.proposals,
            "risksOwn": self.risks_own,
            "coveragePercent": self.coverage_percent,
            "coverageActive": self.coverage_active,
            "coverageTotal": self.coverage_total,
        }


def _coverage_threshold(days: int) -> int:
    """The active-chat threshold pro-rated to a window of ``days`` days.

    One knob (``ACTIVE_CHAT_MIN_MESSAGES``, defined per ~30-day month) stretched
    honestly: a week needs ~a third of the messages a month does. Never below 1.
    """
    return max(1, math.ceil(settings.ACTIVE_CHAT_MIN_MESSAGES * days / 30))


def _window_point(
    scope: ScopeDays,
    start: date,
    end: date,  # exclusive
    today: date,
    floor: date | None,
    *,
    bucket_end: date | None = None,
    test_until: date | None = None,
) -> TrendPoint:
    """Sum a scope over [start, end) — clipped to today — into one point."""
    effective_end = min(end, today + timedelta(days=1))
    counted_days = max(0, (effective_end - start).days)
    last_day = effective_end - timedelta(days=1)

    totals = DayCounters()
    day = start
    while day < effective_end:
        found = scope.counters.get(day)
        if found is not None:
            totals.add(found)
        day += timedelta(days=1)

    threshold = _coverage_threshold(counted_days) if counted_days else 1
    active = 0
    total = 0
    for chat_id, created in scope.chat_created.items():
        if created > last_day:
            continue  # a chat born after this window can't drag its rate down
        total += 1
        per_day = scope.chat_messages.get(chat_id, {})
        messages = sum(
            count for day_key, count in per_day.items() if start <= day_key < effective_end
        )
        if messages >= threshold:
            active += 1

    stored_end = bucket_end if bucket_end is not None else end
    return TrendPoint(
        start=start,
        end=stored_end,
        # A bucket whose (exclusive) end lies beyond today is still accumulating.
        partial=stored_end > today,
        truncated=floor is not None and start < floor,
        test=test_until is not None and start < test_until,
        sla_met=totals.sla_met,
        sla_rated=totals.sla_rated,
        offline=totals.offline,
        proposals=totals.proposals,
        risks_own=totals.risks_own,
        coverage_active=active,
        coverage_total=total,
    )


def build_scope_trends(
    scope: ScopeDays,
    *,
    today: date,
    floor: date | None,
    test_until: date | None = None,
) -> dict[str, dict[str, Any]]:
    """All three granularities for one scope: bucket series + the to-date base.

    The series runs from the bucket containing the horizon floor (or the earliest
    day with data) through the bucket containing today. The LAST bucket is the
    current one, marked partial — its values are already "to date", so tiles read
    it directly; ``prevToDate`` supplies the like-for-like base, or ``null`` when
    that base would reach past the floor and be a lie.
    """
    data_days = set(scope.counters)
    for per_day in scope.chat_messages.values():
        data_days.update(per_day)
    earliest = min(data_days) if data_days else today
    series_floor = floor if floor is not None else earliest

    out: dict[str, dict[str, Any]] = {}
    for granularity in Granularity:
        first = bucket_start(min(series_floor, today), granularity)
        current = bucket_start(today, granularity)
        buckets: list[TrendPoint] = []
        cursor = first
        while cursor <= current:
            end = next_bucket_start(cursor, granularity)
            buckets.append(
                _window_point(scope, cursor, end, today, floor, test_until=test_until)
            )
            cursor = end

        elapsed = (today - current).days + 1
        prev_start = prev_bucket_start(current, granularity)
        prev_to_date: TrendPoint | None = None
        if floor is None or prev_start >= floor:
            prev_to_date = _window_point(
                scope,
                prev_start,
                prev_start + timedelta(days=elapsed),
                # The base window lies wholly in the past; nothing to clip.
                today=today,
                floor=floor,
                bucket_end=prev_start + timedelta(days=elapsed),
                test_until=test_until,
            )

        out[granularity.value] = {
            "buckets": [b.to_payload() for b in buckets],
            "prevToDate": prev_to_date.to_payload() if prev_to_date else None,
        }
    return out


def build_scope_days(
    manager_ids: list[UUID],
    *,
    sla_dated: dict[UUID, list[tuple[datetime, SlaOutcome]]],
    proposal_days: list[dict[str, Any]],
    risk_days: list[dict[str, Any]],
    manager_index: dict[int, UUID],
    chat_day_rows: list[dict[str, Any]],
    chat_registry: list[dict[str, Any]],
    tz: ZoneInfo,
) -> dict[UUID | None, ScopeDays]:
    """Distribute raw day-level inputs into per-manager scopes plus the team.

    Returned dict maps manager id -> scope, and ``None`` -> the team scope. The
    team is fed the same counter increments, so team = sum of managers by
    construction rather than by a second code path that could drift.
    """
    scopes: dict[UUID | None, ScopeDays] = {None: ScopeDays()}
    for manager_id in manager_ids:
        scopes[manager_id] = ScopeDays()

    def bump(manager: UUID, day: date, **inc: int) -> None:
        for scope_key in (manager, None):
            scope = scopes.get(scope_key)
            if scope is None:
                continue
            counters = scope.day_counters(day)
            for name, value in inc.items():
                setattr(counters, name, getattr(counters, name) + value)

    for manager_id, pairs in sla_dated.items():
        for started_at, outcome in pairs:
            day = started_at.astimezone(tz).date()
            if outcome is SlaOutcome.OFFLINE:
                bump(manager_id, day, offline=1)
            else:
                bump(manager_id, day, sla_rated=1, sla_met=1 if outcome.is_met else 0)

    for row in proposal_days:
        bump(row["manager_id"], row["day"], proposals=row["proposals"])

    for row in risk_days:
        attribution, _ = attribute_risk(row["sender_id"], manager_index)
        if attribution.counts:
            bump(row["manager_id"], row["day"], risks_own=1)

    for row in chat_registry:
        created = row["created_at"].astimezone(tz).date()
        for scope_key in (row["manager_id"], None):
            scope = scopes.get(scope_key)
            if scope is not None:
                scope.chat_created[row["chat_id"]] = created

    for row in chat_day_rows:
        for scope_key in (row["manager_id"], None):
            scope = scopes.get(scope_key)
            if scope is not None:
                per_day = scope.chat_messages.setdefault(row["chat_id"], {})
                per_day[row["day"]] = per_day.get(row["day"], 0) + row["messages"]

    return scopes
