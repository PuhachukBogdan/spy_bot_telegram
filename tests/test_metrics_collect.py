"""SLA pairing over conversations + the active-chat coverage fold. No DB."""

from __future__ import annotations

from datetime import UTC, datetime, time, timedelta
from typing import Any
from uuid import UUID, uuid4

from src.metrics.collect import (
    ChatCoverage,
    ManagerMetrics,
    assemble,
    coverage_by_manager,
    pair_waits,
)
from src.metrics.sla import SlaOutcome, SlaThresholds
from src.metrics.workhours import EffectiveWorkHours, WorkHoursSource
from src.utils.workhours import WorkHours

LIMITS = SlaThresholds(
    threshold_seconds=120,
    substantive_grace_seconds=300,
    substantive_reply_chars=200,
    offline_after_seconds=1200,
)
HOURS = EffectiveWorkHours(
    hours=WorkHours(start=time(9, 0), end=time(18, 0), timezone="Europe/Kyiv"),
    source=WorkHoursSource.PERSONAL,
)

MANAGER = uuid4()
CHAT = uuid4()
OTHER_CHAT = uuid4()
# 2026-08-10 is a Monday. 09:00 UTC == 12:00 Kyiv, mid-workday.
BASE = datetime(2026, 8, 10, 9, 0, tzinfo=UTC)


def _msg(
    offset_seconds: int,
    role: str,
    *,
    chars: int = 10,
    chat: UUID = CHAT,
    manager: UUID | None = MANAGER,
    when: datetime | None = None,
) -> dict[str, Any]:
    return {
        "chat_id": chat,
        "timestamp": (when or BASE) + timedelta(seconds=offset_seconds),
        "sender_role": role,
        "sender_id": 1 if role == "internal" else 999,
        "chars": chars,
        "manager_id": manager,
    }


def _pair(messages: list[dict[str, Any]]) -> list[SlaOutcome]:
    return pair_waits(messages, {MANAGER: HOURS}, thresholds=LIMITS).get(MANAGER, [])


def test_fast_reply() -> None:
    assert _pair([_msg(0, "partner"), _msg(60, "internal")]) == [SlaOutcome.MET]


def test_slow_reply_is_missed() -> None:
    assert _pair([_msg(0, "partner"), _msg(400, "internal")]) == [SlaOutcome.MISSED]


def test_long_reply_gets_grace() -> None:
    outcomes = _pair([_msg(0, "partner"), _msg(240, "internal", chars=500)])
    assert outcomes == [SlaOutcome.MET_SUBSTANTIVE]


def test_burst_of_partner_messages_is_one_wait() -> None:
    # Five lines in twenty seconds is ONE question. Counting five would measure
    # how talkative the partner is, not how fast the manager answered.
    burst = [_msg(i * 5, "partner") for i in range(5)]
    assert _pair([*burst, _msg(60, "internal")]) == [SlaOutcome.MET]


def test_burst_timer_starts_at_the_first_message() -> None:
    burst = [_msg(0, "partner"), _msg(100, "partner")]
    # Reply 130s after the FIRST message (30s after the last) — still a miss.
    assert _pair([*burst, _msg(130, "internal")]) == [SlaOutcome.MISSED]


def test_unanswered_conversation_is_offline() -> None:
    assert _pair([_msg(0, "partner")]) == [SlaOutcome.OFFLINE]


def test_two_separate_questions_are_two_waits() -> None:
    outcomes = _pair(
        [
            _msg(0, "partner"),
            _msg(30, "internal"),
            _msg(600, "partner"),
            _msg(1000, "internal"),
        ]
    )
    assert outcomes == [SlaOutcome.MET, SlaOutcome.MISSED]


def test_open_wait_does_not_leak_into_the_next_chat() -> None:
    outcomes = pair_waits(
        [
            _msg(0, "partner", chat=CHAT),
            _msg(0, "partner", chat=OTHER_CHAT),
            _msg(30, "internal", chat=OTHER_CHAT),
        ],
        {MANAGER: HOURS},
        thresholds=LIMITS,
    )
    # First chat never got a reply; the second one's fast reply must not close it.
    assert sorted(outcomes[MANAGER]) == sorted([SlaOutcome.OFFLINE, SlaOutcome.MET])


def test_internal_message_without_a_pending_wait_is_ignored() -> None:
    assert _pair([_msg(0, "internal"), _msg(60, "internal")]) == []


def test_night_message_starts_no_timer() -> None:
    night = datetime(2026, 8, 10, 1, 0, tzinfo=UTC)  # 04:00 Kyiv
    assert _pair([_msg(0, "partner", when=night)]) == []


def test_weekend_message_starts_no_timer() -> None:
    saturday = datetime(2026, 8, 15, 9, 0, tzinfo=UTC)
    assert _pair([_msg(0, "partner", when=saturday)]) == []


def test_holiday_message_starts_no_timer() -> None:
    from datetime import date

    # 2026-08-24 is a Monday: a normal working day unless the calendar says so.
    rows = [_msg(0, "partner", when=datetime(2026, 8, 24, 9, tzinfo=UTC))]
    assert pair_waits(rows, {MANAGER: HOURS}, thresholds=LIMITS)[MANAGER] == [
        SlaOutcome.OFFLINE
    ]
    with_calendar = pair_waits(
        rows,
        {MANAGER: HOURS},
        holidays=frozenset({date(2026, 8, 24)}),
        thresholds=LIMITS,
    )
    assert with_calendar == {}


def test_chat_without_a_resolvable_manager_is_skipped() -> None:
    rows = [_msg(0, "partner", manager=None), _msg(60, "internal", manager=None)]
    assert pair_waits(rows, {MANAGER: HOURS}, thresholds=LIMITS) == {}


def test_manager_without_known_hours_is_skipped() -> None:
    assert pair_waits([_msg(0, "partner")], {}, thresholds=LIMITS) == {}


# ---------------------------------------------------------------------------
# coverage
# ---------------------------------------------------------------------------


def _chat_row(manager: UUID, messages: int) -> dict[str, Any]:
    return {"manager_id": manager, "chat_id": uuid4(), "messages": messages}


def test_coverage_counts_active_over_total() -> None:
    rows = [_chat_row(MANAGER, n) for n in (0, 3, 10, 40, 9)]
    cov = coverage_by_manager(rows, min_messages=10)[MANAGER]
    assert cov.total == 5
    assert cov.active == 2
    assert cov.percent == 40.0


def test_silent_chats_stay_in_the_denominator() -> None:
    # An INNER JOIN upstream would drop these and make everyone look 100%.
    cov = coverage_by_manager([_chat_row(MANAGER, 0)] * 3, min_messages=10)[MANAGER]
    assert cov == ChatCoverage(total=3, active=0)
    assert cov.percent == 0.0


def test_threshold_is_inclusive() -> None:
    cov = coverage_by_manager([_chat_row(MANAGER, 10)], min_messages=10)[MANAGER]
    assert cov.active == 1


def test_empty_portfolio_percent_is_none() -> None:
    assert ChatCoverage().percent is None


# ---------------------------------------------------------------------------
# assemble
# ---------------------------------------------------------------------------


class _Manager:
    def __init__(self, name: str) -> None:
        self.id = uuid4()
        self.full_name = name


def test_managers_with_no_data_still_appear() -> None:
    quiet, busy = _Manager("Quiet"), _Manager("Busy")
    out = assemble(
        [quiet, busy],
        coverage={busy.id: ChatCoverage(total=4, active=2)},
        sla_outcomes={busy.id: [SlaOutcome.MET]},
        proposals={busy.id: 3},
        hours={busy.id: HOURS},
    )
    assert [m.name for m in out] == ["Quiet", "Busy"]
    empty = out[0]
    assert isinstance(empty, ManagerMetrics)
    assert empty.coverage.percent is None
    assert empty.sla.percent is None
    assert empty.proposals == 0
    assert out[1].sla.percent == 100.0
