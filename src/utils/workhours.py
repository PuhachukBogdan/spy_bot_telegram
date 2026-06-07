"""Working-hours parsing + work-minute arithmetic for the operational_sla track.

Two pure helpers, no I/O:
  * :func:`parse_work_hours` validates the ``/set_hours`` input
    (``HH:MM-HH:MM Timezone``) into a :class:`WorkHours`; case- and
    space-tolerant on the time range, case-insensitive on the IANA timezone.
  * :func:`elapsed_work_minutes` counts only the minutes between two instants
    that fall inside the daily working window — the measure the SLA job uses so
    an overnight gap or a weekend doesn't count as an unanswered partner message.

Timezones come from :mod:`zoneinfo`; the ``tzdata`` package supplies the IANA
database on platforms without a system copy (Windows, slim containers).
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import datetime, time, timedelta
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError, available_timezones

# "HH:MM-HH:MM" with 1-2 digit hours, lenient whitespace around the dash.
_RANGE_RE = re.compile(r"^(\d{1,2}):(\d{2})\s*-\s*(\d{1,2}):(\d{2})$")

# Lazily-built case-folded map of IANA names, so "europe/kiev" resolves to the
# canonical "Europe/Kiev" (zoneinfo keys are case-sensitive).
_TZ_BY_LOWER: dict[str, str] | None = None


@dataclass(frozen=True)
class WorkHours:
    """A validated working window: ``start``/``end`` local times + IANA timezone."""

    start: time
    end: time
    timezone: str  # canonical IANA key, e.g. "Europe/Kiev"


def resolve_timezone(name: str) -> ZoneInfo | None:
    """Resolve an IANA timezone name to a :class:`ZoneInfo`, case-insensitively.

    Tries the name as given first (the common, correctly-cased case), then falls
    back to a case-folded lookup so a user typing ``europe/kiev`` still works.
    Returns ``None`` for an unknown or malformed name.
    """
    name = name.strip()
    if not name:
        return None
    try:
        return ZoneInfo(name)
    except (ZoneInfoNotFoundError, ValueError):
        pass

    global _TZ_BY_LOWER
    if _TZ_BY_LOWER is None:
        _TZ_BY_LOWER = {tz.lower(): tz for tz in available_timezones()}
    canonical = _TZ_BY_LOWER.get(name.lower())
    if canonical is None:
        return None
    try:
        return ZoneInfo(canonical)
    except (ZoneInfoNotFoundError, ValueError):
        return None


def parse_work_hours(text: str) -> WorkHours | None:
    """Parse ``HH:MM-HH:MM Timezone`` (e.g. ``09:00-18:00 Europe/Kiev``).

    Returns ``None`` if the format, the time values, or the timezone is invalid,
    so the caller can show the usage hint. The time range is parsed case- and
    space-insensitively; the timezone is resolved case-insensitively against the
    IANA database. ``start`` must be strictly before ``end`` — overnight shifts
    are unsupported in the MVP and rejected here (so :func:`elapsed_work_minutes`
    can assume a same-day window).
    """
    parts = text.strip().split()
    if len(parts) < 2:
        return None
    # The timezone is always the final whitespace-free token (IANA names have no
    # spaces); everything before it is the time range, joined so spaces around the
    # dash ("08:30 - 17:30") collapse to the canonical "08:30-17:30".
    tz_token = parts[-1]
    range_token = "".join(parts[:-1]).lower()

    match = _RANGE_RE.match(range_token)
    if match is None:
        return None
    start_h, start_m, end_h, end_m = (int(g) for g in match.groups())
    if not (0 <= start_h < 24 and 0 <= end_h < 24):
        return None
    if not (0 <= start_m < 60 and 0 <= end_m < 60):
        return None

    start, end = time(start_h, start_m), time(end_h, end_m)
    if start >= end:
        return None

    tz = resolve_timezone(tz_token)
    if tz is None:
        return None
    return WorkHours(start=start, end=end, timezone=str(tz))


def elapsed_work_minutes(
    start_dt: datetime, end_dt: datetime, work: WorkHours
) -> int:
    """Whole minutes between ``start_dt`` and ``end_dt`` inside the work window.

    Both instants are converted to the work timezone; only time inside
    ``[work.start, work.end]`` on each calendar day is counted, so a partner
    message at 17:55 answered at 09:05 the next morning counts ~10 work-minutes,
    not ~15 hours. Returns 0 when ``end_dt`` is not after ``start_dt``. Both
    datetimes should be timezone-aware (DB ``timestamptz`` decodes to aware UTC).
    """
    if end_dt <= start_dt:
        return 0
    tz = resolve_timezone(work.timezone)
    if tz is None:
        return 0

    cur = start_dt.astimezone(tz)
    end = end_dt.astimezone(tz)
    total = 0
    day = cur.date()
    while day <= end.date():
        window_start = datetime.combine(day, work.start, tzinfo=tz)
        window_end = datetime.combine(day, work.end, tzinfo=tz)
        lo = max(window_start, cur)
        hi = min(window_end, end)
        if hi > lo:
            total += int((hi - lo).total_seconds() // 60)
        day = day + timedelta(days=1)
    return total
