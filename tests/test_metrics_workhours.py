"""The personal-vs-default working-hours switch. Pure functions, no DB."""

from __future__ import annotations

from datetime import UTC, datetime, time
from uuid import uuid4

from src.db.models import InternalUser
from src.metrics.workhours import (
    EffectiveWorkHours,
    WorkHoursSource,
    default_work_hours,
    resolve_effective_work_hours,
)
from src.utils.workhours import WorkHours

_DEFAULT = WorkHours(start=time(9, 0), end=time(18, 0), timezone="Europe/Kyiv")


def _user(
    start: time | None = None,
    end: time | None = None,
    timezone: str = "UTC",
) -> InternalUser:
    return InternalUser(
        id=uuid4(),
        full_name="Mirror | Betonwin",
        role="manager",
        telegram_accounts=[8592696398],
        work_hours_start=start,
        work_hours_end=end,
        work_timezone=timezone,
        created_at=datetime.now(UTC),
    )


def test_personal_hours_win_when_set() -> None:
    eff = resolve_effective_work_hours(
        _user(time(8, 30), time(17, 30), "Europe/Kiev"), default=_DEFAULT
    )
    assert eff.source is WorkHoursSource.PERSONAL
    assert eff.is_assumed is False
    assert eff.hours.start == time(8, 30)
    assert eff.hours.end == time(17, 30)
    assert eff.hours.timezone == "Europe/Kiev"


def test_default_used_when_nothing_set() -> None:
    # The measured reality on 2026-08-15: 3 of 4 managers looked like this.
    eff = resolve_effective_work_hours(_user(), default=_DEFAULT)
    assert eff.source is WorkHoursSource.DEFAULT
    assert eff.is_assumed is True
    assert eff.hours == _DEFAULT


def test_half_filled_personal_record_falls_back_entirely() -> None:
    # Blending a personal start with a company end produces a window belonging to
    # nobody — silently wrong in a way nobody could spot in a report.
    only_start = resolve_effective_work_hours(_user(start=time(8, 0)), default=_DEFAULT)
    only_end = resolve_effective_work_hours(_user(end=time(17, 0)), default=_DEFAULT)
    assert only_start.hours == _DEFAULT
    assert only_end.hours == _DEFAULT
    assert only_start.source is WorkHoursSource.DEFAULT
    assert only_end.source is WorkHoursSource.DEFAULT


def test_inverted_personal_range_falls_back() -> None:
    eff = resolve_effective_work_hours(_user(time(18, 0), time(9, 0)), default=_DEFAULT)
    assert eff.source is WorkHoursSource.DEFAULT


def test_unresolvable_personal_timezone_falls_back() -> None:
    eff = resolve_effective_work_hours(
        _user(time(9, 0), time(18, 0), "Mars/Olympus"), default=_DEFAULT
    )
    assert eff.source is WorkHoursSource.DEFAULT
    assert eff.hours == _DEFAULT


def test_legacy_timezone_alias_is_accepted_and_canonicalised() -> None:
    # Christopher's row carries the legacy alias 'Europe/Kiev'; it must not be
    # treated as broken and silently downgraded to the default.
    eff = resolve_effective_work_hours(
        _user(time(9, 0), time(18, 0), "europe/kiev"), default=_DEFAULT
    )
    assert eff.source is WorkHoursSource.PERSONAL
    assert eff.hours.timezone == "Europe/Kiev"


def test_switch_flips_the_moment_hours_are_set() -> None:
    # Same person before and after running /set_hours — no migration, no list.
    before = resolve_effective_work_hours(_user(), default=_DEFAULT)
    after = resolve_effective_work_hours(
        _user(time(10, 0), time(19, 0), "Europe/Kyiv"), default=_DEFAULT
    )
    assert before.is_assumed is True
    assert after.is_assumed is False
    assert after.hours.start == time(10, 0)


def test_default_comes_from_settings() -> None:
    d = default_work_hours()
    assert d.start == time(9, 0)
    assert d.end == time(18, 0)
    assert d.timezone == "Europe/Kyiv"


def test_settings_default_is_used_when_none_passed() -> None:
    eff = resolve_effective_work_hours(_user())
    assert eff.source is WorkHoursSource.DEFAULT
    assert eff.hours == default_work_hours()


def test_effective_hours_is_frozen() -> None:
    eff = EffectiveWorkHours(hours=_DEFAULT, source=WorkHoursSource.PERSONAL)
    assert eff.is_assumed is False
