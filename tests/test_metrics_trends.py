"""Trend folding: bucket boundaries, to-date comparison, coverage pro-rating.

Pure functions, no DB. Dates are deliberately concrete — bucket math is exactly
the kind of code where an off-by-one hides behind an abstraction.
"""

from __future__ import annotations

from datetime import UTC, date, datetime
from typing import Any
from uuid import uuid4
from zoneinfo import ZoneInfo

from src.metrics.sla import SlaOutcome
from src.metrics.trends import (
    DayCounters,
    Granularity,
    ScopeDays,
    bucket_start,
    build_scope_days,
    build_scope_trends,
    next_bucket_start,
    prev_bucket_start,
)

KYIV = ZoneInfo("Europe/Kyiv")
MANAGER = uuid4()
CHAT = uuid4()

# 2026-08-15 is a Saturday; its ISO week starts Monday 2026-08-10.
TODAY = date(2026, 8, 15)


# ---------------------------------------------------------------------------
# bucket arithmetic
# ---------------------------------------------------------------------------


def test_week_buckets_start_monday() -> None:
    assert bucket_start(date(2026, 8, 15), Granularity.WEEK) == date(2026, 8, 10)
    assert bucket_start(date(2026, 8, 10), Granularity.WEEK) == date(2026, 8, 10)
    assert next_bucket_start(date(2026, 8, 10), Granularity.WEEK) == date(2026, 8, 17)
    assert prev_bucket_start(date(2026, 8, 10), Granularity.WEEK) == date(2026, 8, 3)


def test_month_buckets_are_calendar() -> None:
    assert bucket_start(date(2026, 8, 15), Granularity.MONTH) == date(2026, 8, 1)
    assert next_bucket_start(date(2026, 12, 1), Granularity.MONTH) == date(2027, 1, 1)
    assert prev_bucket_start(date(2026, 1, 1), Granularity.MONTH) == date(2025, 12, 1)


def test_quarter_buckets_are_calendar() -> None:
    assert bucket_start(date(2026, 8, 15), Granularity.QUARTER) == date(2026, 7, 1)
    assert bucket_start(date(2026, 3, 31), Granularity.QUARTER) == date(2026, 1, 1)
    assert next_bucket_start(date(2026, 10, 1), Granularity.QUARTER) == date(2027, 1, 1)
    assert prev_bucket_start(date(2026, 7, 1), Granularity.QUARTER) == date(2026, 4, 1)


# ---------------------------------------------------------------------------
# folding
# ---------------------------------------------------------------------------


def _scope(counters: dict[date, DayCounters]) -> ScopeDays:
    return ScopeDays(counters=counters)


def _counters(met: int = 0, rated: int = 0, offline: int = 0) -> DayCounters:
    return DayCounters(sla_met=met, sla_rated=rated, offline=offline)


def test_buckets_sum_counters_and_percent_comes_from_sums() -> None:
    # Two days inside one week: 1/2 and 3/4 → the week is 4/6 = 66.7%, NOT the
    # average of 50% and 75% (62.5%). Averaging percentages is the lie this
    # module exists to prevent.
    scope = _scope(
        {
            date(2026, 8, 10): _counters(met=1, rated=2),
            date(2026, 8, 11): _counters(met=3, rated=4),
        }
    )
    weeks = build_scope_trends(scope, today=TODAY, floor=date(2026, 8, 1))["week"]
    current = weeks["buckets"][-1]
    assert current["slaMet"] == 4
    assert current["slaRated"] == 6
    assert current["slaPercent"] == 66.7


def test_current_bucket_is_partial_and_past_is_not() -> None:
    scope = _scope({date(2026, 8, 5): _counters(met=1, rated=1)})
    weeks = build_scope_trends(scope, today=TODAY, floor=date(2026, 8, 1))["week"]
    by_start = {b["start"]: b for b in weeks["buckets"]}
    assert by_start["2026-08-10"]["partial"] is True  # week of today
    assert by_start["2026-08-03"]["partial"] is False


def test_bucket_started_before_floor_is_truncated() -> None:
    # Floor mid-week: the week bucket containing the floor is marked, the next
    # one is clean. A truncated number without the mark is worse than no number.
    scope = _scope({date(2026, 8, 5): _counters(met=1, rated=1)})
    weeks = build_scope_trends(scope, today=TODAY, floor=date(2026, 8, 5))["week"]
    by_start = {b["start"]: b for b in weeks["buckets"]}
    assert by_start["2026-08-03"]["truncated"] is True
    assert by_start["2026-08-10"]["truncated"] is False


def test_to_date_base_is_same_elapsed_days() -> None:
    # Today is Saturday, 6 elapsed days of the current week. The base must be
    # the FIRST 6 days of last week — not the whole of it: last Sunday's numbers
    # would otherwise punish a week that simply isn't over.
    scope = _scope(
        {
            date(2026, 8, 3): _counters(met=1, rated=1),  # prev Mon — in base
            date(2026, 8, 9): _counters(met=5, rated=5),  # prev Sun — NOT in base
            date(2026, 8, 10): _counters(met=2, rated=2),  # current week
        }
    )
    weeks = build_scope_trends(scope, today=TODAY, floor=date(2026, 8, 1))["week"]
    base = weeks["prevToDate"]
    assert base is not None
    assert base["slaRated"] == 1  # Sunday excluded
    assert base["start"] == "2026-08-03"
    assert base["end"] == "2026-08-09"  # 6 elapsed days, exclusive end


def test_to_date_base_hidden_when_it_reaches_past_the_floor() -> None:
    scope = _scope({date(2026, 8, 12): _counters(met=1, rated=1)})
    weeks = build_scope_trends(scope, today=TODAY, floor=date(2026, 8, 10))["week"]
    assert weeks["prevToDate"] is None  # base would start 08-03 < floor


def test_quarter_base_is_hidden_within_retention() -> None:
    # 120-day retention: floor ~mid-April; the previous quarter starts April 1 —
    # before the floor → no quarter-over-quarter delta, by design (§11.5).
    scope = _scope({date(2026, 8, 12): _counters(met=1, rated=1)})
    quarters = build_scope_trends(scope, today=TODAY, floor=date(2026, 4, 17))
    assert quarters["quarter"]["prevToDate"] is None


def test_offline_stays_out_of_rated() -> None:
    scope = _scope({date(2026, 8, 12): _counters(met=1, rated=1, offline=7)})
    weeks = build_scope_trends(scope, today=TODAY, floor=date(2026, 8, 1))["week"]
    current = weeks["buckets"][-1]
    assert current["offline"] == 7
    assert current["slaRated"] == 1
    assert current["slaPercent"] == 100.0


# ---------------------------------------------------------------------------
# coverage: pro-rated threshold + denominator bounded by chat age
# ---------------------------------------------------------------------------


def _chat_scope(
    per_day: dict[date, int], created: date, chat: Any = None
) -> ScopeDays:
    chat_id = chat or CHAT
    return ScopeDays(
        chat_messages={chat_id: per_day},
        chat_created={chat_id: created},
    )


def test_week_threshold_is_pro_rated() -> None:
    # 10 msgs/month → ceil(10 × 6/30) = 2 for the 6 elapsed days of this week.
    scope = _chat_scope({date(2026, 8, 11): 2}, created=date(2026, 6, 1))
    weeks = build_scope_trends(scope, today=TODAY, floor=date(2026, 8, 1))["week"]
    current = weeks["buckets"][-1]
    assert current["coverageActive"] == 1
    assert current["coverageTotal"] == 1

    barely_under = _chat_scope({date(2026, 8, 11): 1}, created=date(2026, 6, 1))
    weeks = build_scope_trends(barely_under, today=TODAY, floor=date(2026, 8, 1))["week"]
    assert weeks["buckets"][-1]["coverageActive"] == 0


def test_chat_born_later_is_not_in_earlier_denominators() -> None:
    # A chat created Aug 12 must not drag down the week of Aug 3 — it did not
    # exist then, so its silence back then is not a fact about anyone.
    scope = _chat_scope({date(2026, 8, 12): 30}, created=date(2026, 8, 12))
    weeks = build_scope_trends(scope, today=TODAY, floor=date(2026, 8, 1))["week"]
    by_start = {b["start"]: b for b in weeks["buckets"]}
    assert by_start["2026-08-03"]["coverageTotal"] == 0
    assert by_start["2026-08-10"]["coverageTotal"] == 1
    assert by_start["2026-08-10"]["coverageActive"] == 1


def test_coverage_percent_null_when_no_chats_existed() -> None:
    scope = _chat_scope({date(2026, 8, 12): 5}, created=date(2026, 8, 12))
    weeks = build_scope_trends(scope, today=TODAY, floor=date(2026, 8, 1))["week"]
    by_start = {b["start"]: b for b in weeks["buckets"]}
    assert by_start["2026-08-03"]["coveragePercent"] is None


# ---------------------------------------------------------------------------
# scope distribution: team = sum of managers by construction
# ---------------------------------------------------------------------------


def test_team_scope_sums_manager_counters() -> None:
    other = uuid4()
    chat_a, chat_b = uuid4(), uuid4()
    created = datetime(2026, 6, 1, tzinfo=UTC)
    scopes = build_scope_days(
        [MANAGER, other],
        sla_dated={
            MANAGER: [(datetime(2026, 8, 12, 9, tzinfo=UTC), SlaOutcome.MET)],
            other: [
                (datetime(2026, 8, 12, 9, tzinfo=UTC), SlaOutcome.MISSED),
                (datetime(2026, 8, 12, 10, tzinfo=UTC), SlaOutcome.OFFLINE),
            ],
        },
        proposal_days=[{"manager_id": MANAGER, "day": date(2026, 8, 12), "proposals": 3}],
        risk_days=[
            {"manager_id": MANAGER, "sender_id": 111, "day": date(2026, 8, 12)},
            {"manager_id": other, "sender_id": 999, "day": date(2026, 8, 12)},
        ],
        manager_index={111: MANAGER},
        chat_day_rows=[
            {"manager_id": MANAGER, "chat_id": chat_a, "day": date(2026, 8, 12), "messages": 4},
            {"manager_id": other, "chat_id": chat_b, "day": date(2026, 8, 12), "messages": 1},
        ],
        chat_registry=[
            {"manager_id": MANAGER, "chat_id": chat_a, "created_at": created},
            {"manager_id": other, "chat_id": chat_b, "created_at": created},
        ],
        tz=KYIV,
    )
    team = scopes[None].counters[date(2026, 8, 12)]
    assert (team.sla_met, team.sla_rated, team.offline) == (1, 2, 1)
    assert team.proposals == 3
    assert team.risks_own == 1  # the partner-raised case (999) never counts
    assert len(scopes[None].chat_created) == 2
    assert len(scopes[MANAGER].chat_created) == 1


def test_sla_day_is_the_wait_start_in_kyiv() -> None:
    # 22:30 UTC on the 12th is already 01:30 on the 13th in Kyiv (UTC+3 summer).
    scopes = build_scope_days(
        [MANAGER],
        sla_dated={MANAGER: [(datetime(2026, 8, 12, 22, 30, tzinfo=UTC), SlaOutcome.MET)]},
        proposal_days=[],
        risk_days=[],
        manager_index={},
        chat_day_rows=[],
        chat_registry=[],
        tz=KYIV,
    )
    assert date(2026, 8, 13) in scopes[MANAGER].counters
    assert date(2026, 8, 12) not in scopes[MANAGER].counters


# ---------------------------------------------------------------------------
# day granularity + the test-period flag
# ---------------------------------------------------------------------------


def test_day_buckets_are_single_days() -> None:
    assert bucket_start(date(2026, 8, 15), Granularity.DAY) == date(2026, 8, 15)
    assert next_bucket_start(date(2026, 8, 15), Granularity.DAY) == date(2026, 8, 16)
    assert prev_bucket_start(date(2026, 8, 15), Granularity.DAY) == date(2026, 8, 14)


def test_day_series_covers_the_floor_to_today() -> None:
    scope = _scope({date(2026, 8, 12): _counters(met=1, rated=1)})
    days = build_scope_trends(scope, today=TODAY, floor=date(2026, 8, 10))["day"]
    starts = [b["start"] for b in days["buckets"]]
    assert starts[0] == "2026-08-10"
    assert starts[-1] == "2026-08-15"
    assert len(starts) == 6
    # Yesterday is the day-granularity base: full previous single-day bucket.
    assert days["prevToDate"] is not None
    assert days["prevToDate"]["start"] == "2026-08-14"


def test_test_period_flags_buckets_before_the_cutoff() -> None:
    scope = _scope({date(2026, 8, 12): _counters(met=1, rated=1)})
    weeks = build_scope_trends(
        scope, today=TODAY, floor=date(2026, 7, 27), test_until=date(2026, 8, 10)
    )["week"]
    by_start = {b["start"]: b for b in weeks["buckets"]}
    assert by_start["2026-08-03"]["test"] is True  # starts before the cutoff
    assert by_start["2026-08-10"]["test"] is False


def test_no_cutoff_means_nothing_is_flagged() -> None:
    scope = _scope({date(2026, 8, 12): _counters(met=1, rated=1)})
    weeks = build_scope_trends(scope, today=TODAY, floor=date(2026, 8, 1))["week"]
    assert all(b["test"] is False for b in weeks["buckets"])
