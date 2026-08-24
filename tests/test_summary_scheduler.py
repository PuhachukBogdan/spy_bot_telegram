"""Tests for the in-process summary scheduler (replaces the old n8n cron).

Covers the cron-occurrence math and one scheduler tick's fire/skip decisions.
No real DB, Slack, or network — collaborators are monkeypatched.
"""

from __future__ import annotations

from datetime import UTC, date, datetime, timedelta
from types import SimpleNamespace
from typing import Any
from zoneinfo import ZoneInfo

import pytest

from src.pipeline import workers as w

# June 2026: the 1st is a Monday, so the 15th and 22nd are Mondays too;
# the 24th (today) is a Wednesday.
#
# Slots fire at 00:00 in REPORT_TIMEZONE (Kyiv = UTC+3 in summer, UTC+2 in
# winter), so the expected UTC instant is 21:00 / 22:00 the PREVIOUS day.
_KYIV = ZoneInfo("Europe/Kyiv")


# ---------------------------------------------------------------------------
# Occurrence math
# ---------------------------------------------------------------------------


def test_weekly_occurrence_is_prior_monday_local_midnight() -> None:
    now = datetime(2026, 6, 24, 15, 0, tzinfo=UTC)  # Wednesday 15:00 UTC
    occ = w._last_weekly_occurrence(now)
    assert occ == datetime(2026, 6, 21, 21, 0, tzinfo=UTC)  # Mon 00:00 Kyiv (+3)
    local = occ.astimezone(_KYIV)
    assert local.weekday() == 0 and local.hour == 0
    assert occ <= now and (now - occ) < timedelta(days=7)


def test_weekly_occurrence_before_local_midnight_steps_back_a_week() -> None:
    # Sunday 22:00 Kyiv — this week's Monday 00:00 has not struck yet.
    now = datetime(2026, 6, 21, 19, 0, tzinfo=UTC)
    occ = w._last_weekly_occurrence(now)
    assert occ == datetime(2026, 6, 14, 21, 0, tzinfo=UTC)  # previous Mon 00:00 Kyiv


def test_monthly_occurrence_is_first_of_month_local_midnight() -> None:
    now = datetime(2026, 6, 24, 15, 0, tzinfo=UTC)
    occ = w._last_monthly_occurrence(now)
    assert occ == datetime(2026, 5, 31, 21, 0, tzinfo=UTC)  # 1 Jun 00:00 Kyiv
    assert occ.astimezone(_KYIV).day == 1


def test_monthly_occurrence_before_local_midnight_steps_to_prev_month() -> None:
    # 31 May 22:00 Kyiv — June's slot hasn't struck; fall back to 1 May.
    now = datetime(2026, 5, 31, 19, 0, tzinfo=UTC)
    occ = w._last_monthly_occurrence(now)
    assert occ == datetime(2026, 4, 30, 21, 0, tzinfo=UTC)  # 1 May 00:00 Kyiv


def test_occurrences_hold_local_midnight_across_dst() -> None:
    """Winter slots land at 22:00 UTC, summer at 21:00 — local midnight either way."""
    winter = w._last_weekly_occurrence(datetime(2026, 1, 21, 12, 0, tzinfo=UTC))
    summer = w._last_weekly_occurrence(datetime(2026, 7, 22, 12, 0, tzinfo=UTC))
    for occ in (winter, summer):
        local = occ.astimezone(_KYIV)
        assert (local.hour, local.minute, local.weekday()) == (0, 0, 0)
    assert winter.hour == 22 and summer.hour == 21


def test_bad_timezone_falls_back_to_utc(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(w.settings, "REPORT_TIMEZONE", "Mars/Olympus_Mons")
    assert w.report_timezone() is UTC
    occ = w._last_weekly_occurrence(datetime(2026, 6, 24, 15, 0, tzinfo=UTC))
    assert occ == datetime(2026, 6, 22, 0, 0, tzinfo=UTC)  # Monday 00:00 UTC


# ---------------------------------------------------------------------------
# run_summary_scheduler_tick
# ---------------------------------------------------------------------------


class _NullConn:
    async def __aenter__(self) -> None:
        return None

    async def __aexit__(self, *exc: object) -> bool:
        return False


@pytest.fixture
def patched_tick(monkeypatch: pytest.MonkeyPatch) -> dict[str, Any]:
    rec: dict[str, Any] = {
        "generated": [],
        "until": [],
        "refreshed": [],
        "refresh_until": [],
        "exists": False,
    }

    async def fake_exists(
        conn: Any, period_type: str, since: Any, *, delivered_only: bool = False
    ) -> bool:
        return bool(rec["exists"])

    async def fake_generate(
        *, period_type: str, until: datetime | None = None
    ) -> Any:
        rec["generated"].append(period_type)
        rec["until"].append(until)
        return SimpleNamespace(
            url="https://x/r/abc", event_count=3, slack_delivered=True
        )

    async def fake_refresh(
        *, period_type: str, until: datetime | None = None
    ) -> int:
        rec["refreshed"].append(period_type)
        rec["refresh_until"].append(until)
        return 7

    monkeypatch.setattr(w, "summary_exists_since", fake_exists)
    monkeypatch.setattr(w, "generate_report", fake_generate)
    monkeypatch.setattr(w, "refresh_report", fake_refresh)
    monkeypatch.setattr(w, "acquire_connection", lambda: _NullConn())
    # Default: today's midnight is long past, so the daily refresh stays out of
    # the way. Tests that exercise it override this.
    monkeypatch.setattr(
        w, "_last_daily_occurrence", lambda now: now - timedelta(hours=10)
    )
    return rec


async def test_tick_fires_due_weekly_skips_stale_monthly(
    patched_tick: dict[str, Any], monkeypatch: pytest.MonkeyPatch
) -> None:
    # Weekly slot 5 min ago → within window; monthly slot 10h ago → too old.
    monkeypatch.setattr(
        w, "_last_weekly_occurrence", lambda now: now - timedelta(minutes=5)
    )
    monkeypatch.setattr(
        w, "_last_monthly_occurrence", lambda now: now - timedelta(hours=10)
    )
    fired = await w.run_summary_scheduler_tick()
    assert fired == ["weekly"]
    assert patched_tick["generated"] == ["weekly"]


async def test_tick_pins_window_end_to_the_scheduled_slot(
    patched_tick: dict[str, Any], monkeypatch: pytest.MonkeyPatch
) -> None:
    """The window ends at the SLOT, not at now() — else tick jitter opens a gap
    between consecutive reports that no report covers."""
    slots: list[datetime] = []

    def weekly(now: datetime) -> datetime:
        occ = now - timedelta(minutes=5)  # due, inside the catch-up window
        slots.append(occ)
        return occ

    monkeypatch.setattr(w, "_last_weekly_occurrence", weekly)
    monkeypatch.setattr(
        w, "_last_monthly_occurrence", lambda now: now - timedelta(hours=10)
    )
    await w.run_summary_scheduler_tick()
    assert patched_tick["generated"] == ["weekly"]
    assert patched_tick["until"] == slots  # the slot instant, NOT now()


async def test_tick_skips_already_generated_slot(
    patched_tick: dict[str, Any], monkeypatch: pytest.MonkeyPatch
) -> None:
    patched_tick["exists"] = True  # a row already exists since the slot
    monkeypatch.setattr(
        w, "_last_weekly_occurrence", lambda now: now - timedelta(minutes=5)
    )
    monkeypatch.setattr(
        w, "_last_monthly_occurrence", lambda now: now - timedelta(minutes=5)
    )
    fired = await w.run_summary_scheduler_tick()
    assert fired == []
    assert patched_tick["generated"] == []


async def test_tick_skips_when_no_slot_in_window(
    patched_tick: dict[str, Any], monkeypatch: pytest.MonkeyPatch
) -> None:
    # Both slots older than the 6h catch-up window → nothing fires.
    monkeypatch.setattr(
        w, "_last_weekly_occurrence", lambda now: now - timedelta(hours=10)
    )
    monkeypatch.setattr(
        w, "_last_monthly_occurrence", lambda now: now - timedelta(hours=10)
    )
    fired = await w.run_summary_scheduler_tick()
    assert fired == []


# ---------------------------------------------------------------------------
# Daily content refresh
# ---------------------------------------------------------------------------


def _no_releases_due(monkeypatch: pytest.MonkeyPatch) -> None:
    """No weekly/monthly release in the catch-up window (an ordinary weekday)."""
    monkeypatch.setattr(
        w, "_last_weekly_occurrence", lambda now: now - timedelta(days=3)
    )
    monkeypatch.setattr(
        w, "_last_monthly_occurrence", lambda now: now - timedelta(days=12)
    )


async def test_tick_refreshes_both_types_on_an_ordinary_day(
    patched_tick: dict[str, Any], monkeypatch: pytest.MonkeyPatch
) -> None:
    """Midweek: nothing is released, but content is re-rendered so yesterday's
    events show up on the link already posted in Slack."""
    _no_releases_due(monkeypatch)
    slots: list[datetime] = []

    def daily(now: datetime) -> datetime:
        occ = now - timedelta(minutes=30)  # local midnight, inside the window
        slots.append(occ)
        return occ

    monkeypatch.setattr(w, "_last_daily_occurrence", daily)
    fired = await w.run_summary_scheduler_tick()
    assert fired == ["weekly:refresh", "monthly:refresh"]
    assert patched_tick["generated"] == []  # no Slack post, no new link
    assert patched_tick["refreshed"] == ["weekly", "monthly"]
    # Both refreshes cover the window ending at that midnight.
    assert patched_tick["refresh_until"] == [slots[0], slots[0]]


async def test_tick_does_not_refresh_a_type_released_this_tick(
    patched_tick: dict[str, Any], monkeypatch: pytest.MonkeyPatch
) -> None:
    """On a Monday the weekly release and the daily slot are the same instant, so
    refreshing weekly again would upsert the just-released row back to 'pending'
    and undo the release."""
    # Monday: the weekly slot and the daily slot resolve to the same instant
    # (the tick computes `now` once, so both lambdas see the same value).
    def slot(now: datetime) -> datetime:
        return now - timedelta(minutes=5)

    monkeypatch.setattr(w, "_last_weekly_occurrence", slot)
    monkeypatch.setattr(w, "_last_daily_occurrence", slot)
    monkeypatch.setattr(
        w, "_last_monthly_occurrence", lambda now: now - timedelta(hours=10)
    )
    fired = await w.run_summary_scheduler_tick()
    assert fired == ["weekly", "monthly:refresh"]
    assert patched_tick["refreshed"] == ["monthly"]  # weekly NOT refreshed


async def test_tick_skips_refresh_when_content_already_fresh(
    patched_tick: dict[str, Any], monkeypatch: pytest.MonkeyPatch
) -> None:
    _no_releases_due(monkeypatch)
    monkeypatch.setattr(w, "_last_daily_occurrence", lambda now: now)
    patched_tick["exists"] = True  # a row already exists since midnight
    fired = await w.run_summary_scheduler_tick()
    assert fired == []
    assert patched_tick["refreshed"] == []


async def test_tick_skips_refresh_after_a_late_start(
    patched_tick: dict[str, Any], monkeypatch: pytest.MonkeyPatch
) -> None:
    """Booted long after local midnight → don't back-fill a stale refresh."""
    _no_releases_due(monkeypatch)
    monkeypatch.setattr(
        w, "_last_daily_occurrence", lambda now: now - timedelta(hours=10)
    )
    fired = await w.run_summary_scheduler_tick()
    assert fired == []
    assert patched_tick["refreshed"] == []


def test_daily_occurrence_is_local_midnight_today() -> None:
    now = datetime(2026, 8, 4, 9, 30, tzinfo=UTC)  # 12:30 Kyiv
    occ = w._last_daily_occurrence(now)
    assert occ == datetime(2026, 8, 3, 21, 0, tzinfo=UTC)
    local = occ.astimezone(_KYIV)
    assert (local.date(), local.hour) == (date(2026, 8, 4), 0)
    assert occ <= now
