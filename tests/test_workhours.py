"""Unit tests for working-hours parsing + work-minute arithmetic.

Pure functions (src/utils/workhours.py) — no DB / network. Timezone resolution
relies on the IANA database (the ``tzdata`` dependency on Windows).
"""

from __future__ import annotations

from datetime import UTC, datetime, time
from zoneinfo import ZoneInfo

from src.utils.workhours import (
    WorkHours,
    elapsed_work_minutes,
    parse_work_hours,
    resolve_timezone,
)

# --- resolve_timezone --------------------------------------------------------


def test_resolve_exact_name() -> None:
    tz = resolve_timezone("Europe/Kiev")
    assert tz is not None
    assert str(tz) == "Europe/Kiev"


def test_resolve_is_case_insensitive() -> None:
    tz = resolve_timezone("europe/kiev")
    assert tz is not None
    assert str(tz) == "Europe/Kiev"


def test_resolve_unknown_is_none() -> None:
    assert resolve_timezone("Mars/Phobos") is None
    assert resolve_timezone("") is None


# --- parse_work_hours --------------------------------------------------------


def test_parse_valid() -> None:
    wh = parse_work_hours("09:00-18:00 Europe/Kiev")
    assert wh == WorkHours(start=time(9, 0), end=time(18, 0), timezone="Europe/Kiev")


def test_parse_tolerates_case_and_spacing() -> None:
    wh = parse_work_hours("08:30 - 17:30 asia/almaty")
    assert wh is not None
    assert wh.start == time(8, 30)
    assert wh.end == time(17, 30)
    assert wh.timezone == "Asia/Almaty"


def test_parse_rejects_bad_format() -> None:
    assert parse_work_hours("9am to 6pm Kiev") is None
    assert parse_work_hours("09:00-18:00") is None  # no timezone
    assert parse_work_hours("") is None


def test_parse_rejects_out_of_range_times() -> None:
    assert parse_work_hours("25:00-26:00 UTC") is None
    assert parse_work_hours("09:70-18:00 UTC") is None


def test_parse_rejects_non_positive_window() -> None:
    assert parse_work_hours("18:00-09:00 UTC") is None  # overnight unsupported
    assert parse_work_hours("09:00-09:00 UTC") is None  # zero-length


def test_parse_rejects_unknown_timezone() -> None:
    assert parse_work_hours("09:00-18:00 Mars/Phobos") is None


# --- elapsed_work_minutes ----------------------------------------------------

_WORK = WorkHours(start=time(9, 0), end=time(18, 0), timezone="UTC")


def test_elapsed_within_one_day() -> None:
    start = datetime(2026, 6, 8, 10, 0, tzinfo=UTC)
    end = datetime(2026, 6, 8, 10, 30, tzinfo=UTC)
    assert elapsed_work_minutes(start, end, _WORK) == 30


def test_elapsed_clamps_to_window() -> None:
    # Spans 17:55 -> 09:05 next day: only 5 min before close + 5 min after open.
    start = datetime(2026, 6, 8, 17, 55, tzinfo=UTC)
    end = datetime(2026, 6, 9, 9, 5, tzinfo=UTC)
    assert elapsed_work_minutes(start, end, _WORK) == 10


def test_elapsed_ignores_time_outside_window() -> None:
    # Entirely after hours (20:00 -> 22:00): zero work minutes.
    start = datetime(2026, 6, 8, 20, 0, tzinfo=UTC)
    end = datetime(2026, 6, 8, 22, 0, tzinfo=UTC)
    assert elapsed_work_minutes(start, end, _WORK) == 0


def test_elapsed_zero_when_end_not_after_start() -> None:
    t = datetime(2026, 6, 8, 10, 0, tzinfo=UTC)
    assert elapsed_work_minutes(t, t, _WORK) == 0
    earlier = datetime(2026, 6, 8, 9, 0, tzinfo=UTC)
    assert elapsed_work_minutes(t, earlier, _WORK) == 0


def test_elapsed_respects_timezone() -> None:
    # Kyiv is UTC+3 in June (DST). 06:30Z = 09:30 Kyiv -> 30 work-min to 07:00Z.
    work = WorkHours(start=time(9, 0), end=time(18, 0), timezone="Europe/Kiev")
    start = datetime(2026, 6, 8, 6, 30, tzinfo=UTC)
    end = datetime(2026, 6, 8, 7, 0, tzinfo=UTC)
    assert elapsed_work_minutes(start, end, work) == 30
    # 05:00Z = 08:00 Kyiv (before open) -> the 06:00Z..06:00Z+ part counts only
    # from 09:00 Kyiv (06:00Z).
    pre_open = datetime(2026, 6, 8, 5, 0, tzinfo=UTC)
    at_open = datetime(2026, 6, 8, 6, 0, tzinfo=UTC)
    assert elapsed_work_minutes(pre_open, at_open, work) == 0


def test_resolve_timezone_returns_zoneinfo() -> None:
    assert isinstance(resolve_timezone("UTC"), ZoneInfo)
