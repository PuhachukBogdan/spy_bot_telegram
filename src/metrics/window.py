"""Metrics windows and the epoch floor. Pure and synchronous — no I/O.

Two rules from PHASE2_MANAGER_KPI.md §0.2 live here, and only here:

1. **No window ever starts before the epoch.** Phase 2 KPIs count forward from
   the day the code was published; history is a separate backfill job.
2. **A comparison base that is not fully inside the measured era is no base.**
   Showing a delta against a half-empty previous window invents growth that did
   not happen — worse than showing nothing, because it looks like a measurement.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, date, datetime, timedelta


def _as_utc(moment: datetime) -> datetime:
    """Treat a naive datetime as UTC rather than raising deep inside a comparison.

    Report code paths mix values that came from asyncpg (tz-aware) with ones built
    in Python, and a naive/aware comparison raises ``TypeError`` at the point of
    use — which, in a scheduled job, surfaces as a report that silently failed to
    generate. Normalising here keeps that failure impossible.
    """
    return moment.replace(tzinfo=UTC) if moment.tzinfo is None else moment.astimezone(UTC)


def epoch_floor(epoch: date | None) -> datetime | None:
    """The epoch as an instant: 00:00 UTC on that date. ``None`` stays ``None``."""
    if epoch is None:
        return None
    return datetime.combine(epoch, datetime.min.time(), tzinfo=UTC)


@dataclass(frozen=True)
class MetricsWindow:
    """A measured period plus, when one exists, a comparable preceding period."""

    since: datetime
    until: datetime
    previous: tuple[datetime, datetime] | None

    @property
    def has_comparison(self) -> bool:
        """False when deltas must be hidden — no base, not a base of zero."""
        return self.previous is not None

    @property
    def is_empty(self) -> bool:
        """True when the whole requested window predates the epoch."""
        return self.since >= self.until

    @property
    def length(self) -> timedelta:
        return self.until - self.since


def resolve_metrics_window(
    requested_since: datetime,
    until: datetime,
    *,
    epoch: date | None,
) -> MetricsWindow:
    """Clamp a requested window to the epoch and derive its comparison base.

    The comparison base is the window of equal length ending exactly where this
    one starts, so consecutive periods tile without gap or overlap. Length is
    measured AFTER clamping: if the request was cut short by the epoch, the base
    matches what is actually being shown, not what was asked for.

    Returns a window with ``previous=None`` — meaning "hide the deltas" — when
    that base would reach back past the epoch, and an empty window when the whole
    request does.
    """
    floor = epoch_floor(epoch)
    since = _as_utc(requested_since)
    until = _as_utc(until)
    if floor is not None and since < floor:
        since = floor

    if since >= until:
        return MetricsWindow(since=until, until=until, previous=None)

    previous_start = since - (until - since)
    previous = (
        None
        if floor is not None and previous_start < floor
        else (previous_start, since)
    )
    return MetricsWindow(since=since, until=until, previous=previous)
