"""Chat membership: crews, the crew-rule SLA attribution, the account split, the
sweep. Pure functions plus a sweep run against a fake bot and a fake connection
— no DB, no Telegram.

The scenarios are the ones that broke the owner model on 2026-10-06: a group
with four managers in it, the head present almost everywhere but working little,
a departed manager who still formally owned chats his colleagues were answering.
"""

from __future__ import annotations

from contextlib import asynccontextmanager
from datetime import UTC, date, datetime, time, timedelta
from types import SimpleNamespace
from typing import Any
from uuid import UUID, uuid4
from zoneinfo import ZoneInfo

import pytest

from src.bot.handlers.dm_commands import _parse_deactivate_args
from src.db.queries.chat_members import StaffAccount, SweepTarget
from src.metrics.accounts import account_days_payload, account_stats, label_accounts
from src.metrics.attribution import RiskAttribution
from src.metrics.collect import by_manager, pair_waits_all, risks_by_manager
from src.metrics.membership import Crews, build_crews, fan_out_by_portfolio
from src.metrics.scope import PageScope, visible_memberships, visible_sender_rows
from src.metrics.sla import SlaOutcome, SlaThresholds
from src.metrics.trends import build_scope_days, build_scope_trends
from src.metrics.workhours import EffectiveWorkHours, WorkHoursSource
from src.pipeline import membership as sweep
from src.utils.workhours import WorkHours

KYIV = ZoneInfo("Europe/Kyiv")
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
EARLY = EffectiveWorkHours(
    hours=WorkHours(start=time(6, 0), end=time(11, 0), timezone="Europe/Kyiv"),
    source=WorkHoursSource.PERSONAL,
)

ANNA, BORIS, HEAD, GONE = uuid4(), uuid4(), uuid4(), uuid4()
ANNA_OLD, ANNA_NEW, BORIS_TG, HEAD_TG, GONE_TG, ADMIN_TG = 11, 12, 21, 31, 41, 91
INDEX: dict[int, UUID] = {
    ANNA_OLD: ANNA,
    ANNA_NEW: ANNA,
    BORIS_TG: BORIS,
    HEAD_TG: HEAD,
    GONE_TG: GONE,
}
ROSTER = [ANNA, BORIS, HEAD, GONE]
CHAT, OTHER = uuid4(), uuid4()
# Monday 2026-08-10, 12:00 Kyiv.
BASE = datetime(2026, 8, 10, 9, 0, tzinfo=UTC)
DAY = date(2026, 8, 10)


def _membership(*pairs: tuple[UUID, int]) -> list[dict[str, Any]]:
    return [
        {"chat_id": c, "telegram_user_id": tg, "internal_user_id": INDEX[tg]} for c, tg in pairs
    ]


def _activity(*rows: tuple[int, UUID, date]) -> list[dict[str, Any]]:
    return [
        {"sender_id": tg, "chat_id": c, "day": d, "messages": 1, "last_at": None}
        for tg, c, d in rows
    ]


def _crews(
    membership: list[dict[str, Any]],
    activity: list[dict[str, Any]],
    *,
    owner: UUID | None = None,
    deactivated: dict[UUID, date] | None = None,
) -> Crews:
    return build_crews(
        membership,
        activity,
        [{"chat_id": CHAT, "owner_id": owner}, {"chat_id": OTHER, "owner_id": None}],
        manager_index=INDEX,
        roster=ROSTER,
        deactivated=deactivated or {},
        lookback_days=30,
    )


def _msg(
    offset: int, role: str, *, sender: int = 999, chars: int = 10, chat: UUID = CHAT
) -> dict[str, Any]:
    return {
        "chat_id": chat,
        "timestamp": BASE + timedelta(seconds=offset),
        "sender_role": role,
        "sender_id": sender,
        "chars": chars,
        "owner_id": None,
    }


# ---------------------------------------------------------------------------
# crews
# ---------------------------------------------------------------------------


def test_crew_is_present_and_recently_active_only() -> None:
    crews = _crews(
        _membership((CHAT, ANNA_OLD), (CHAT, BORIS_TG), (CHAT, HEAD_TG)),
        _activity(
            (ANNA_NEW, CHAT, DAY - timedelta(days=3)), (HEAD_TG, CHAT, DAY - timedelta(days=60))
        ),
    )
    # Anna wrote last week (from her other account — accounts fold onto the
    # person); the head wrote two months ago; Boris never did.
    assert crews.crew(CHAT, DAY) == {ANNA}
    # Everyone present is in the portfolio regardless of activity.
    assert crews.members_of(CHAT) == {ANNA, BORIS, HEAD}
    assert crews.portfolio(HEAD) == {CHAT}


def test_crew_falls_back_to_owner_then_to_nobody() -> None:
    crews = _crews(_membership((CHAT, BORIS_TG)), [], owner=BORIS)
    assert crews.crew(CHAT, DAY) == {BORIS}
    # An owner who is not on the roster is no fallback.
    not_roster = _crews(_membership((CHAT, BORIS_TG)), [], owner=uuid4())
    assert not_roster.crew(CHAT, DAY) == frozenset()


def test_deactivation_cuts_crew_and_portfolio_from_that_day() -> None:
    cut = DAY
    crews = _crews(
        _membership((CHAT, GONE_TG), (CHAT, ANNA_OLD)),
        _activity(
            (GONE_TG, CHAT, DAY - timedelta(days=1)), (ANNA_OLD, CHAT, DAY - timedelta(days=1))
        ),
        owner=GONE,
        deactivated={GONE: cut},
    )
    assert crews.crew(CHAT, cut - timedelta(days=1)) == {GONE, ANNA}
    assert crews.crew(CHAT, cut) == {ANNA}
    assert crews.portfolio(GONE, as_of=cut) == set()
    assert crews.portfolio(GONE, as_of=cut - timedelta(days=1)) == {CHAT}
    # Owner fallback never resurrects a deactivated person either.
    alone = _crews(_membership((CHAT, GONE_TG)), [], owner=GONE, deactivated={GONE: cut})
    assert alone.crew(CHAT, cut) == frozenset()


def test_fan_out_copies_a_chat_row_once_per_present_manager() -> None:
    crews = _crews(_membership((CHAT, ANNA_OLD), (CHAT, HEAD_TG)), [])
    rows = fan_out_by_portfolio(
        [{"chat_id": CHAT, "messages": 5}, {"chat_id": OTHER, "messages": 1}], crews
    )
    assert sorted(r["manager_id"] for r in rows) == sorted([ANNA, HEAD])
    assert all(r["chat_id"] == CHAT for r in rows)


# ---------------------------------------------------------------------------
# pairing under the crew rule
# ---------------------------------------------------------------------------

HOURS_BY = {ANNA: HOURS, BORIS: HOURS, HEAD: HOURS, GONE: HOURS}


def _pair(messages: list[dict[str, Any]], crews: Crews, **kw: Any) -> list[Any]:
    return pair_waits_all(
        messages,
        HOURS_BY,
        crews=crews,
        manager_index=INDEX,
        tz=KYIV,
        default_hours=HOURS.hours,
        thresholds=LIMITS,
        **kw,
    )


def test_answered_wait_is_credited_to_the_answerer_not_the_crew() -> None:
    crews = _crews(
        _membership((CHAT, ANNA_OLD), (CHAT, BORIS_TG)),
        _activity(
            (ANNA_OLD, CHAT, DAY - timedelta(days=1)), (BORIS_TG, CHAT, DAY - timedelta(days=1))
        ),
    )
    waits = _pair([_msg(0, "partner"), _msg(60, "internal", sender=BORIS_TG)], crews)
    assert len(waits) == 1
    assert waits[0].outcome is SlaOutcome.MET
    assert waits[0].answered_by == BORIS
    assert waits[0].answered_with == BORIS_TG
    assert waits[0].managers == {BORIS}
    assert by_manager(waits) == {BORIS: [(BASE, SlaOutcome.MET)]}


def test_unanswered_wait_is_charged_to_the_whole_crew_on_duty() -> None:
    crews = _crews(
        _membership((CHAT, ANNA_OLD), (CHAT, BORIS_TG), (CHAT, HEAD_TG)),
        _activity(
            (ANNA_OLD, CHAT, DAY - timedelta(days=1)), (BORIS_TG, CHAT, DAY - timedelta(days=1))
        ),
    )
    # Nobody answers inside 20 min; the head is present but never wrote here.
    waits = _pair([_msg(0, "partner"), _msg(1500, "internal", sender=ANNA_OLD)], crews)
    assert waits[0].outcome is SlaOutcome.OFFLINE
    assert waits[0].charged == {ANNA, BORIS}
    assert waits[0].answered_by is None
    folded = by_manager(waits)
    assert set(folded) == {ANNA, BORIS}


def test_off_duty_crew_member_is_not_charged() -> None:
    crews = _crews(
        _membership((CHAT, ANNA_OLD), (CHAT, BORIS_TG)),
        _activity(
            (ANNA_OLD, CHAT, DAY - timedelta(days=1)), (BORIS_TG, CHAT, DAY - timedelta(days=1))
        ),
    )
    hours = {**HOURS_BY, BORIS: EARLY}  # Boris's day ends at 11:00; the wait is at 12:00
    waits = pair_waits_all(
        [_msg(0, "partner")],
        hours,
        crews=crews,
        manager_index=INDEX,
        tz=KYIV,
        default_hours=HOURS.hours,
        thresholds=LIMITS,
    )
    assert waits[0].charged == {ANNA}


def test_reply_by_someone_not_measured_counts_for_the_team_only() -> None:
    crews = _crews(_membership((CHAT, ANNA_OLD)), _activity((ANNA_OLD, CHAT, DAY)))
    waits = _pair([_msg(0, "partner"), _msg(30, "internal", sender=ADMIN_TG)], crews)
    assert len(waits) == 1
    assert waits[0].answered_by is None
    assert waits[0].answered_with == ADMIN_TG
    assert waits[0].managers == frozenset()
    assert by_manager(waits) == {}


def test_hidden_accounts_reply_removes_the_wait_entirely() -> None:
    crews = _crews(
        _membership((CHAT, ANNA_OLD), (CHAT, HEAD_TG)),
        _activity((ANNA_OLD, CHAT, DAY), (HEAD_TG, CHAT, DAY)),
    )
    waits = _pair(
        [_msg(0, "partner"), _msg(30, "internal", sender=HEAD_TG)],
        crews,
        hidden_accounts=frozenset({HEAD_TG}),
    )
    assert waits == []


def test_crewless_chat_opens_a_team_level_wait_with_default_hours() -> None:
    crews = _crews(_membership((OTHER, HEAD_TG)), [])  # nobody active, no owner
    waits = _pair([_msg(0, "partner", chat=OTHER)], crews)
    assert len(waits) == 1
    assert waits[0].outcome is SlaOutcome.OFFLINE
    assert waits[0].managers == frozenset()


def test_owner_rule_is_unchanged_without_crews() -> None:
    rows = [
        {**_msg(0, "partner"), "owner_id": ANNA},
        {**_msg(60, "internal", sender=BORIS_TG), "owner_id": ANNA},
    ]
    waits = pair_waits_all(rows, HOURS_BY, thresholds=LIMITS)
    assert waits[0].answered_by == ANNA  # the owner, whoever replied


# ---------------------------------------------------------------------------
# trends: the team counts a shared chat once
# ---------------------------------------------------------------------------


def test_team_counts_a_shared_wait_and_a_shared_chat_once() -> None:
    crews = _crews(
        _membership((CHAT, ANNA_OLD), (CHAT, BORIS_TG)),
        _activity(
            (ANNA_OLD, CHAT, DAY - timedelta(days=1)), (BORIS_TG, CHAT, DAY - timedelta(days=1))
        ),
    )
    waits = _pair([_msg(0, "partner")], crews)  # offline, charged to both
    registry = [
        {"chat_id": CHAT, "owner_id": None, "created_at": BASE - timedelta(days=30)},
        {"chat_id": OTHER, "owner_id": None, "created_at": BASE - timedelta(days=30)},
    ]
    scopes = build_scope_days(
        ROSTER,
        waits=waits,
        proposal_days=[],
        risk_days=[],
        manager_index=INDEX,
        chat_day_rows=[{"chat_id": CHAT, "day": DAY, "messages": 4}],
        chat_registry=registry,
        tz=KYIV,
        crews=crews,
    )
    assert scopes[None].counters[DAY].offline == 1
    assert scopes[ANNA].counters[DAY].offline == 1
    assert scopes[BORIS].counters[DAY].offline == 1
    assert HEAD not in {k for k, v in scopes.items() if k and v.counters}
    # Both present managers hold the chat; the team holds both chats once each.
    assert set(scopes[ANNA].chat_created) == {CHAT}
    assert set(scopes[BORIS].chat_created) == {CHAT}
    assert set(scopes[None].chat_created) == {CHAT, OTHER}
    assert scopes[ANNA].chat_messages[CHAT][DAY] == 4


def test_deactivated_scope_keeps_history_and_shows_no_portfolio_after() -> None:
    cut = date(2026, 8, 12)
    crews = _crews(_membership((CHAT, GONE_TG)), [], deactivated={GONE: cut})
    scopes = build_scope_days(
        [GONE],
        waits=[],
        proposal_days=[],
        risk_days=[],
        manager_index=INDEX,
        chat_day_rows=[{"chat_id": CHAT, "day": date(2026, 8, 5), "messages": 12}],
        chat_registry=[
            {"chat_id": CHAT, "owner_id": GONE, "created_at": BASE - timedelta(days=60)}
        ],
        tz=KYIV,
        crews=crews,
        deactivated={GONE: cut},
    )
    assert scopes[GONE].active_until == cut - timedelta(days=1)
    days = build_scope_trends(scopes[GONE], today=date(2026, 8, 20), floor=date(2026, 7, 1))["day"]
    by_start = {b["start"]: b for b in days["buckets"]}
    assert by_start["2026-08-05"]["coverageTotal"] == 1  # history stays
    assert by_start["2026-08-05"]["coverageActive"] == 1
    assert by_start["2026-08-12"]["coverageTotal"] == 0  # the cut
    assert by_start["2026-08-19"]["coverageTotal"] == 0


# ---------------------------------------------------------------------------
# risks: own to the author, context to the crew
# ---------------------------------------------------------------------------


def _risk(sender: int | None, when: datetime = BASE) -> dict[str, Any]:
    return {
        "id": uuid4(),
        "chat_id": CHAT,
        "risk_type": "hidden_payment",
        "risk_level": "high",
        "final_score": 70,
        "created_at": when,
        "detected_phrase": "мимо кассы",
        "llm_explanation": "x",
        "sender_id": sender,
        "status": "new",
        "chat_name": "1 | Acme | Stalker",
        "topic_name": None,
        "unit_type": "group",
        "owner_id": GONE,
    }


def test_authored_case_lands_on_the_author_only_and_counts() -> None:
    crews = _crews(
        _membership((CHAT, ANNA_OLD), (CHAT, BORIS_TG)),
        _activity((ANNA_OLD, CHAT, DAY), (BORIS_TG, CHAT, DAY)),
        owner=GONE,
    )
    pages = risks_by_manager([_risk(BORIS_TG)], INDEX, crews=crews, tz=KYIV)
    assert set(pages) == {BORIS}
    assert pages[BORIS][0].attribution is RiskAttribution.MANAGER_ACTION
    assert pages[BORIS][0].counts


def test_partner_case_is_context_for_the_crew_and_never_counts() -> None:
    crews = _crews(
        _membership((CHAT, ANNA_OLD), (CHAT, BORIS_TG), (CHAT, HEAD_TG)),
        _activity((ANNA_OLD, CHAT, DAY), (BORIS_TG, CHAT, DAY)),
        owner=GONE,
    )
    pages = risks_by_manager([_risk(999)], INDEX, crews=crews, tz=KYIV)
    assert set(pages) == {ANNA, BORIS}  # the head is present but not working it
    assert all(not case.counts for cases in pages.values() for case in cases)


def test_owner_rule_no_longer_charges_a_colleagues_case_to_the_owner() -> None:
    pages = risks_by_manager([_risk(BORIS_TG)], INDEX)  # legacy: owner GONE
    assert set(pages) == {GONE}
    assert pages[GONE][0].attribution is RiskAttribution.CHAT_CONTEXT


# ---------------------------------------------------------------------------
# accounts: old / new
# ---------------------------------------------------------------------------


def test_labels_follow_first_sight_unless_pinned() -> None:
    seen = {ANNA_OLD: BASE - timedelta(days=90), ANNA_NEW: BASE - timedelta(days=5)}
    assert label_accounts([ANNA_NEW, ANNA_OLD], {}, seen) == {ANNA_OLD: "old", ANNA_NEW: "new"}
    # Pinned wins; the other follows.
    assert label_accounts([ANNA_OLD, ANNA_NEW], {str(ANNA_NEW): "old"}, seen) == {
        ANNA_NEW: "old",
        ANNA_OLD: "new",
    }
    # Never seen anywhere cannot be the old one.
    assert label_accounts([ANNA_OLD, 77], {}, {77: None, ANNA_OLD: seen[ANNA_OLD]}) == {  # type: ignore[dict-item]
        ANNA_OLD: "old",
        77: "new",
    }
    assert label_accounts([BORIS_TG], {}, {}) == {BORIS_TG: None}


def test_account_stats_split_messages_and_replies_by_account() -> None:
    crews = _crews(
        _membership((CHAT, ANNA_OLD), (CHAT, ANNA_NEW), (OTHER, ANNA_NEW)),
        _activity((ANNA_NEW, CHAT, DAY)),
    )
    waits = _pair(
        [
            _msg(0, "partner"),
            _msg(30, "internal", sender=ANNA_NEW),
            _msg(3600, "partner"),
            _msg(3600 + 900, "internal", sender=ANNA_OLD),  # late → missed, still a reply
        ],
        crews,
    )
    rows = [
        {"sender_id": ANNA_NEW, "chat_id": CHAT, "day": DAY, "messages": 7, "last_at": BASE},
        {
            "sender_id": ANNA_NEW,
            "chat_id": OTHER,
            "day": DAY - timedelta(days=1),
            "messages": 1,
            "last_at": None,
        },
        {"sender_id": ANNA_OLD, "chat_id": CHAT, "day": DAY, "messages": 2, "last_at": BASE},
        {
            "sender_id": ANNA_OLD,
            "chat_id": CHAT,
            "day": DAY - timedelta(days=40),
            "messages": 50,
            "last_at": None,
        },
    ]
    stats = account_stats(
        [ANNA_OLD, ANNA_NEW],
        labels={ANNA_OLD: "old", ANNA_NEW: "new"},
        sender_day_rows=rows,
        waits=waits,
        crews=crews,
        since=BASE - timedelta(days=30),
        tz=KYIV,
    )
    old, new = stats
    assert (old.label, old.messages, old.active_days, old.chats) == ("old", 2, 1, 1)
    assert (old.replies, old.replies_on_time) == (1, 0)
    assert (new.label, new.messages, new.active_days, new.chats) == ("new", 8, 2, 2)
    assert (new.replies, new.replies_on_time) == (1, 1)

    manager = SimpleNamespace(id=ANNA, telegram_accounts=[ANNA_OLD, ANNA_NEW])
    days = account_days_payload(
        [manager],
        labels_by_manager={ANNA: {ANNA_OLD: "old", ANNA_NEW: "new"}},
        sender_day_rows=rows,
        waits=waits,
        tz=KYIV,
    )
    by_account = {e["a"]: e for e in days}
    assert by_account[str(ANNA_NEW)]["d"][DAY.isoformat()] == 7
    assert by_account[str(ANNA_NEW)]["r"][DAY.isoformat()] == 1
    assert by_account[str(ANNA_NEW)]["o"][DAY.isoformat()] == 1
    assert by_account[str(ANNA_OLD)]["r"][DAY.isoformat()] == 1
    assert DAY.isoformat() not in by_account[str(ANNA_OLD)]["o"]


# ---------------------------------------------------------------------------
# scope: the head's accounts leave membership and sender rows
# ---------------------------------------------------------------------------


def test_scope_drops_the_hidden_persons_membership_and_messages() -> None:
    scope = PageScope(
        role="head",
        viewer_id=HEAD,
        hidden_manager_ids=frozenset({HEAD}),
        hidden_telegram_ids=frozenset({HEAD_TG}),
    )
    members = _membership((CHAT, ANNA_OLD), (CHAT, HEAD_TG))
    assert [r["telegram_user_id"] for r in visible_memberships(members, scope)] == [ANNA_OLD]
    rows = _activity((HEAD_TG, CHAT, DAY), (ANNA_OLD, CHAT, DAY))
    assert [r["sender_id"] for r in visible_sender_rows(rows, scope)] == [ANNA_OLD]


# ---------------------------------------------------------------------------
# the sweep
# ---------------------------------------------------------------------------


class _SweepBot:
    """Read-only Telegram stand-in: titles and statuses, nothing sendable."""

    def __init__(self, titles: dict[int, str], statuses: dict[tuple[int, int], str]) -> None:
        self.titles = titles
        self.statuses = statuses
        self.calls: list[tuple[str, Any]] = []

    async def get_chat(self, chat_id: int) -> SimpleNamespace:
        self.calls.append(("get_chat", chat_id))
        return SimpleNamespace(title=self.titles[chat_id], type="supergroup")

    async def get_chat_member(self, chat_id: int, user_id: int) -> SimpleNamespace:
        self.calls.append(("get_chat_member", (chat_id, user_id)))
        return SimpleNamespace(status=self.statuses.get((chat_id, user_id), "left"))

    def __getattr__(self, name: str) -> Any:
        if name.startswith(("send_", "edit_", "pin_", "delete_")):
            raise AssertionError(f"the sweep must never call bot.{name}")
        raise AttributeError(name)


class _Conn:
    pass


async def test_sweep_writes_titles_and_statuses(monkeypatch: pytest.MonkeyPatch) -> None:
    unit_a, unit_b = uuid4(), uuid4()
    targets = [
        SweepTarget(telegram_chat_id=-100, unit_ids=(unit_a,), chat_name="old name"),
        SweepTarget(telegram_chat_id=-200, unit_ids=(unit_b,), chat_name="same"),
    ]
    accounts = [
        StaffAccount(ANNA_OLD, ANNA, "Anna", "manager", True, None),
        StaffAccount(HEAD_TG, HEAD, "Head", "head", True, None),
    ]
    bot = _SweepBot(
        titles={-100: "new name", -200: "same"},
        statuses={(-100, ANNA_OLD): "administrator", (-200, HEAD_TG): "member"},
    )
    written: list[tuple[UUID, int, str]] = []
    titles: list[tuple[int, str]] = []

    @asynccontextmanager
    async def fake_acquire() -> Any:
        yield _Conn()

    async def fake_targets(conn: Any) -> list[SweepTarget]:
        return targets

    async def fake_accounts(conn: Any) -> list[StaffAccount]:
        return accounts

    async def fake_record(conn: Any, **kw: Any) -> None:
        written.append((kw["chat_id"], kw["telegram_user_id"], kw["status"]))

    async def fake_title(conn: Any, chat_id: int, title: str | None) -> int:
        titles.append((chat_id, title or ""))
        return 1

    monkeypatch.setattr(sweep, "acquire_connection", fake_acquire)
    monkeypatch.setattr(sweep, "list_sweep_targets", fake_targets)
    monkeypatch.setattr(sweep, "list_staff_accounts", fake_accounts)
    monkeypatch.setattr(sweep, "record_verified_status", fake_record)
    monkeypatch.setattr(sweep, "update_chat_title", fake_title)

    stats = await sweep.run_membership_sweep(bot, concurrency=2)  # type: ignore[arg-type]

    assert stats.chats == 2 and stats.errors == 0
    assert titles == [(-100, "new name")]  # the unchanged one is not rewritten
    assert stats.titles_updated == 1
    assert sorted(written) == sorted(
        [
            (unit_a, ANNA_OLD, "administrator"),
            (unit_a, HEAD_TG, "left"),
            (unit_b, ANNA_OLD, "left"),
            (unit_b, HEAD_TG, "member"),
        ]
    )
    assert stats.present == 2 and stats.absent == 2
    # 2 getChat + 4 getChatMember, nothing else.
    assert len(bot.calls) == 6


# ---------------------------------------------------------------------------
# /deactivate_user argument parsing
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("1000000001", ("1000000001", None)),
        ("1000000001 left 2026-09-30", ("1000000001", "left 2026-09-30")),
        ('"Kowalski | BetonWin" ушёл, не уволен', ("Kowalski | BetonWin", "ушёл, не уволен")),
        ('"Kowalski | BetonWin"', ("Kowalski | BetonWin", None)),
        ("Mirror", ("Mirror", None)),
        ("", None),
        (None, None),
        ('"unterminated', None),
    ],
)
def test_parse_deactivate_args(raw: str | None, expected: tuple[str, str | None] | None) -> None:
    assert _parse_deactivate_args(raw) == expected


def test_account_scopes_hold_replies_proposals_cases_and_chats_per_account() -> None:
    from src.metrics.trends import build_account_scope_days

    crews = _crews(
        _membership((CHAT, ANNA_OLD), (CHAT, ANNA_NEW), (OTHER, ANNA_NEW)),
        _activity((ANNA_NEW, CHAT, DAY)),
    )
    waits = _pair(
        [
            _msg(0, "partner"),
            _msg(30, "internal", sender=ANNA_NEW),
            _msg(3600, "partner"),  # nobody answers -> offline, never an account's
        ],
        crews,
    )
    registry = [
        {"chat_id": CHAT, "owner_id": None, "created_at": BASE - timedelta(days=30)},
        {"chat_id": OTHER, "owner_id": None, "created_at": BASE - timedelta(days=30)},
    ]
    scopes = build_account_scope_days(
        {ANNA_OLD: ANNA, ANNA_NEW: ANNA},
        waits=waits,
        proposal_sender_days=[{"sender_id": ANNA_OLD, "day": DAY, "proposals": 2}],
        risk_days=[{"sender_id": ANNA_NEW, "day": DAY, "chat_id": CHAT}],
        chat_day_rows=[{"chat_id": CHAT, "day": DAY, "messages": 9}],
        chat_registry=registry,
        crews=crews,
        tz=KYIV,
    )
    old, new = scopes[f"acct:{ANNA_OLD}"], scopes[f"acct:{ANNA_NEW}"]
    assert (new.counters[DAY].sla_rated, new.counters[DAY].sla_met) == (1, 1)
    assert (
        new.counters[DAY].offline == 0 and DAY not in old.counters or old.counters[DAY].offline == 0
    )
    assert old.counters[DAY].proposals == 2
    assert new.counters[DAY].risks_own == 1
    assert set(old.chat_created) == {CHAT}
    assert set(new.chat_created) == {CHAT, OTHER}
    assert new.chat_messages[CHAT][DAY] == 9
