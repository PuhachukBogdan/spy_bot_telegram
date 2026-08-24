"""SLA bands and the timer gate. Pure functions, no DB, no env dependence."""

from __future__ import annotations

from datetime import UTC, date, datetime, time

import pytest

from src.metrics.sla import SlaOutcome, SlaTally, SlaThresholds, classify_response, tally
from src.metrics.workhours import starts_a_timer
from src.utils.workhours import WorkHours

LIMITS = SlaThresholds(
    threshold_seconds=120,
    substantive_grace_seconds=300,
    substantive_reply_chars=200,
    offline_after_seconds=1200,
)
HOURS = WorkHours(start=time(9, 0), end=time(18, 0), timezone="Europe/Kyiv")
LONG = 250
SHORT = 12


def _classify(waited: float | None, chars: int | None = SHORT) -> SlaOutcome:
    return classify_response(waited, chars, thresholds=LIMITS)


# ---------------------------------------------------------------------------
# bands
# ---------------------------------------------------------------------------


def test_fast_reply_is_met() -> None:
    assert _classify(30) is SlaOutcome.MET
    assert _classify(120) is SlaOutcome.MET  # boundary is inclusive


def test_slow_short_reply_is_missed() -> None:
    assert _classify(121) is SlaOutcome.MISSED
    assert _classify(600) is SlaOutcome.MISSED


def test_substantial_reply_earns_the_grace_band() -> None:
    # Typing a real answer takes longer than typing "ок" — without this the
    # metric would reward the fastest possible non-answer.
    assert _classify(240, LONG) is SlaOutcome.MET_SUBSTANTIVE
    assert _classify(300, LONG) is SlaOutcome.MET_SUBSTANTIVE  # cap inclusive


def test_grace_does_not_extend_past_the_cap() -> None:
    assert _classify(301, LONG) is SlaOutcome.MISSED
    assert _classify(900, 5000) is SlaOutcome.MISSED


def test_a_long_reply_inside_the_threshold_is_just_met() -> None:
    assert _classify(60, LONG) is SlaOutcome.MET


def test_reply_at_exactly_the_char_limit_gets_no_grace() -> None:
    # "more than 2-3 sentences" — the limit itself is not more than the limit.
    assert _classify(240, 200) is SlaOutcome.MISSED
    assert _classify(240, 201) is SlaOutcome.MET_SUBSTANTIVE


def test_silence_is_offline_not_a_miss() -> None:
    assert _classify(None) is SlaOutcome.OFFLINE
    assert _classify(1200) is SlaOutcome.OFFLINE
    assert _classify(5000) is SlaOutcome.OFFLINE


def test_offline_wins_over_a_very_late_long_reply() -> None:
    # A reply that finally lands after half an hour is absence, not extreme
    # slowness — otherwise one forgotten chat dominates the average.
    assert _classify(1800, LONG) is SlaOutcome.OFFLINE


def test_outcome_flags() -> None:
    assert SlaOutcome.MET.is_met and SlaOutcome.MET_SUBSTANTIVE.is_met
    assert not SlaOutcome.MISSED.is_met and not SlaOutcome.OFFLINE.is_met
    assert SlaOutcome.OFFLINE.in_ratio is False
    for other in (SlaOutcome.MET, SlaOutcome.MET_SUBSTANTIVE, SlaOutcome.MISSED):
        assert other.in_ratio is True


# ---------------------------------------------------------------------------
# tally
# ---------------------------------------------------------------------------


def test_percentage_excludes_offline() -> None:
    t = tally(
        [SlaOutcome.MET, SlaOutcome.MET_SUBSTANTIVE, SlaOutcome.MISSED]
        + [SlaOutcome.OFFLINE] * 10
    )
    assert t.rated == 3
    assert t.percent == pytest.approx(66.7)
    assert t.offline == 10


def test_no_traffic_is_none_not_zero() -> None:
    # A manager with no partner messages has not failed; 0.0 would read as if
    # he had.
    empty = tally([])
    assert empty.percent is None
    assert tally([SlaOutcome.OFFLINE]).percent is None


def test_perfect_and_total_miss() -> None:
    assert tally([SlaOutcome.MET] * 4).percent == 100.0
    assert tally([SlaOutcome.MISSED] * 4).percent == 0.0


def test_tally_is_frozen_dataclass() -> None:
    assert SlaTally().percent is None


# ---------------------------------------------------------------------------
# the timer gate: weekends, holidays, off-hours
# ---------------------------------------------------------------------------


def _kyiv(day: int, hour: int, minute: int = 0) -> datetime:
    # 2026-08-10 is a Monday; noon Kyiv (UTC+3 in summer) is 09:00 UTC.
    return datetime(2026, 8, day, hour - 3, minute, tzinfo=UTC)


def test_timer_starts_during_the_workday() -> None:
    assert starts_a_timer(_kyiv(10, 12), HOURS) is True
    assert starts_a_timer(_kyiv(10, 9), HOURS) is True  # start is inclusive


def test_timer_does_not_start_outside_hours() -> None:
    assert starts_a_timer(_kyiv(10, 3), HOURS) is False
    assert starts_a_timer(_kyiv(10, 8, 59), HOURS) is False
    assert starts_a_timer(_kyiv(10, 18), HOURS) is False  # end is exclusive
    assert starts_a_timer(_kyiv(10, 22), HOURS) is False


def test_timer_does_not_start_at_the_weekend() -> None:
    assert starts_a_timer(_kyiv(15, 12), HOURS) is False  # Saturday
    assert starts_a_timer(_kyiv(16, 12), HOURS) is False  # Sunday


def test_timer_does_not_start_on_a_holiday() -> None:
    holidays = frozenset({date(2026, 8, 24)})  # a Monday
    assert starts_a_timer(_kyiv(24, 12), HOURS, holidays=holidays) is False
    assert starts_a_timer(_kyiv(24, 12), HOURS) is True  # without the calendar


def test_gate_is_evaluated_in_the_managers_timezone() -> None:
    # 07:00 UTC is 10:00 in Kyiv (inside) but 07:00 in UTC hours (outside).
    moment = datetime(2026, 8, 10, 7, tzinfo=UTC)
    assert starts_a_timer(moment, HOURS) is True
    utc_hours = WorkHours(start=time(9, 0), end=time(18, 0), timezone="UTC")
    assert starts_a_timer(moment, utc_hours) is False


def test_unresolvable_timezone_starts_nothing() -> None:
    broken = WorkHours(start=time(9, 0), end=time(18, 0), timezone="Mars/Olympus")
    assert starts_a_timer(_kyiv(10, 12), broken) is False
