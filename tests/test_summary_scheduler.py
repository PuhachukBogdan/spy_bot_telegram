"""Tests for the in-process summary scheduler (replaces the old n8n cron).

Covers the cron-occurrence math and one scheduler tick's fire/skip decisions.
No real DB, Slack, or network — collaborators are monkeypatched.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from typing import Any

import pytest

from src.pipeline import workers as w

# June 2026: the 1st is a Monday, so the 15th and 22nd are Mondays too;
# the 24th (today) is a Wednesday.


# ---------------------------------------------------------------------------
# Occurrence math
# ---------------------------------------------------------------------------


def test_weekly_occurrence_is_prior_monday_8am() -> None:
    now = datetime(2026, 6, 24, 15, 0, tzinfo=UTC)  # Wednesday 15:00
    occ = w._last_weekly_occurrence(now)
    assert occ == datetime(2026, 6, 22, 8, 0, tzinfo=UTC)
    assert occ.weekday() == 0 and occ.hour == 8
    assert occ <= now and (now - occ) < timedelta(days=7)


def test_weekly_occurrence_before_8am_monday_steps_back_a_week() -> None:
    now = datetime(2026, 6, 22, 6, 0, tzinfo=UTC)  # Monday 06:00, before 08:00
    occ = w._last_weekly_occurrence(now)
    assert occ == datetime(2026, 6, 15, 8, 0, tzinfo=UTC)


def test_monthly_occurrence_is_first_of_month_8am() -> None:
    now = datetime(2026, 6, 24, 15, 0, tzinfo=UTC)
    occ = w._last_monthly_occurrence(now)
    assert occ == datetime(2026, 6, 1, 8, 0, tzinfo=UTC)


def test_monthly_occurrence_before_1st_8am_steps_to_prev_month() -> None:
    now = datetime(2026, 6, 1, 6, 0, tzinfo=UTC)  # 1st, before 08:00
    occ = w._last_monthly_occurrence(now)
    assert occ == datetime(2026, 5, 1, 8, 0, tzinfo=UTC)


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
    rec: dict[str, Any] = {"generated": [], "exists": False}

    async def fake_exists(conn: Any, period_type: str, since: Any) -> bool:
        return bool(rec["exists"])

    async def fake_generate(*, period_type: str) -> Any:
        rec["generated"].append(period_type)
        return SimpleNamespace(
            url="https://x/r/abc", event_count=3, slack_delivered=True
        )

    monkeypatch.setattr(w, "summary_exists_since", fake_exists)
    monkeypatch.setattr(w, "generate_report", fake_generate)
    monkeypatch.setattr(w, "acquire_connection", lambda: _NullConn())
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
