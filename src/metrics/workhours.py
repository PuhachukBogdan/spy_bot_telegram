"""Which working window a manager is measured against. Pure — no I/O.

The switch: **a manager's own hours if they set them, the configured default if
they did not.** It flips per person and per report run, with no migration and no
manual list — the moment someone runs ``/set_hours`` their next report is scored
against their real schedule.

Every result carries :class:`WorkHoursSource`, because the two are not equally
trustworthy. A default window is an *assumption* about when someone works, and a
percentage computed against an assumption must be visibly marked as such — an
unmarked one invites comparing a manager measured on real hours against one
measured on a guess, which is a ranking of paperwork, not performance.
"""

from __future__ import annotations

from collections.abc import Container
from dataclasses import dataclass
from datetime import date, datetime
from enum import StrEnum

from src.config import settings
from src.db.models import InternalUser
from src.utils.workhours import WorkHours, resolve_timezone

#: Saturday and Sunday. ``date.weekday()`` is Monday=0.
_WEEKEND = frozenset({5, 6})


class WorkHoursSource(StrEnum):
    """Where the working window used for a measurement came from."""

    #: The manager set it via /set_hours. Trustworthy.
    PERSONAL = "personal"
    #: Nothing was set; the configured fallback was assumed. Mark it in the UI.
    DEFAULT = "default"


@dataclass(frozen=True)
class EffectiveWorkHours:
    """The window a manager is actually measured against, plus its provenance."""

    hours: WorkHours
    source: WorkHoursSource

    @property
    def is_assumed(self) -> bool:
        """True when the numbers rest on a schedule the manager never confirmed."""
        return self.source is WorkHoursSource.DEFAULT


def default_work_hours() -> WorkHours:
    """The configured fallback window."""
    return WorkHours(
        start=settings.METRICS_DEFAULT_WORK_HOURS_START,
        end=settings.METRICS_DEFAULT_WORK_HOURS_END,
        timezone=settings.METRICS_DEFAULT_WORK_TIMEZONE,
    )


def resolve_effective_work_hours(
    user: InternalUser,
    *,
    default: WorkHours | None = None,
) -> EffectiveWorkHours:
    """Pick the window to measure ``user`` against.

    Personal hours are used only when they are **complete and usable**: both
    ``start`` and ``end`` present, ``start`` before ``end``, and a timezone that
    actually resolves. A half-filled or unresolvable personal record falls back to
    the default *entirely*, rather than being blended with it — a window made of
    one manager's start time and the company's end time belongs to nobody, and
    would be silently wrong in a way no one could spot in a report.

    ``default`` is injectable so callers can test without touching settings.
    """
    fallback = default_work_hours() if default is None else default

    start, end = user.work_hours_start, user.work_hours_end
    if start is None or end is None or start >= end:
        return EffectiveWorkHours(hours=fallback, source=WorkHoursSource.DEFAULT)

    timezone = resolve_timezone(user.work_timezone)
    if timezone is None:
        return EffectiveWorkHours(hours=fallback, source=WorkHoursSource.DEFAULT)

    return EffectiveWorkHours(
        hours=WorkHours(start=start, end=end, timezone=str(timezone)),
        source=WorkHoursSource.PERSONAL,
    )


def starts_a_timer(
    moment: datetime,
    hours: WorkHours,
    *,
    holidays: Container[date] = frozenset(),
) -> bool:
    """Whether a message at ``moment`` should start an SLA timer at all.

    A message arriving at night, at the weekend or on a holiday simply does not
    start one — it is not a fast reply and it is not a slow one, it is outside the
    measured period. Dropping it here is what keeps the rest of the SLA code
    plain wall-clock arithmetic: every timer that exists began inside a workday,
    so there is never an overnight or weekend gap to subtract afterwards.

    Evaluated in the manager's own timezone, so 09:00 means their morning.
    """
    timezone = resolve_timezone(hours.timezone)
    if timezone is None:
        return False

    local = moment.astimezone(timezone)
    if local.date() in holidays or local.weekday() in _WEEKEND:
        return False
    return hours.start <= local.time() < hours.end
