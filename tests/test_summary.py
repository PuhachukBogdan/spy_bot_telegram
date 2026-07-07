"""Tests for the HTML report builder and generator. Phase 16.

No real DB, Slack, or network. The builder is pure-function; the generator is
tested with monkeypatched collaborators.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any
from uuid import uuid4

import pytest

from src.summary import generator as gen_mod
from src.summary.builder import (
    RISK_CATEGORIES,
    _manager_label,
    _risk_type_label,
    build_report_html,
)

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

_SINCE = datetime(2026, 6, 2, tzinfo=UTC)
_UNTIL = datetime(2026, 6, 9, tzinfo=UTC)


def _mgr(tg_username: str = "test_mgr", aff_id: str | None = None) -> dict[str, Any]:
    return {
        "id": uuid4(), "full_name": "Test Manager", "tg_username": tg_username, "aff_id": aff_id
    }


def _event_row(
    manager_id: Any,
    risk_level: str = "high",
    risk_type: str = "shadow_deal",
    partner_name: str = "Acme",
    score: int = 72,
    author_name: str | None = "Иван Петров",
    author_role: str | None = "internal",
) -> dict[str, Any]:
    return {
        "id": uuid4(),
        "manager_id": manager_id,
        "risk_level": risk_level,
        "risk_type": risk_type,
        "final_score": score,
        "partner_name": partner_name,
        "detected_phrase": "между нами",
        "llm_explanation": "Off-the-books deal suspected.",
        "author_name": author_name,
        "author_role": author_role,
        "status": "open",
        "created_at": datetime(2026, 6, 5, 14, 32, tzinfo=UTC),
    }


def _heatmap_row(manager_id: Any, risk_type: str = "shadow_deal", cnt: int = 1) -> dict[str, Any]:
    return {"manager_id": manager_id, "risk_type": risk_type, "cnt": cnt}


# ---------------------------------------------------------------------------
# _risk_type_label
# ---------------------------------------------------------------------------


def test_known_risk_type_label() -> None:
    assert _risk_type_label("shadow_deal") == "Shadow Deal"
    assert _risk_type_label("data_leak") == "Data Leak"


def test_unknown_risk_type_falls_back_to_title() -> None:
    assert _risk_type_label("some_new_type") == "Some New Type"


# ---------------------------------------------------------------------------
# _manager_label — never renders a bare "@" (pilot bug)
# ---------------------------------------------------------------------------


def test_manager_label_aff_and_username() -> None:
    assert _manager_label({"aff_id": "78516", "tg_username": "anna_k"}) == "78516 | @anna_k"


def test_manager_label_username_only() -> None:
    assert _manager_label({"aff_id": None, "tg_username": "elena_m"}) == "@elena_m"


def test_manager_label_aff_only() -> None:
    assert _manager_label({"aff_id": "91203", "tg_username": None}) == "91203"


def test_manager_label_falls_back_to_full_name() -> None:
    # Neither aff_id nor tg_username — must NOT render bare "@".
    label = _manager_label(
        {"aff_id": None, "tg_username": None, "full_name": "Игорь Петров"}
    )
    assert label == "Игорь Петров"


def test_manager_label_last_resort_placeholder() -> None:
    assert _manager_label({}) == "Unassigned"


# ---------------------------------------------------------------------------
# build_report_html — empty
# ---------------------------------------------------------------------------


def test_build_empty_report_is_valid_html() -> None:
    html = build_report_html(
        period_type="weekly",
        since=_SINCE,
        until=_UNTIL,
        managers=[],
        heatmap_rows=[],
        event_rows=[],
    )
    assert html.startswith("<!DOCTYPE html>")
    assert "Weekly Risk Report" in html
    assert "02 Jun 2026" in html
    assert "09 Jun 2026" in html


def test_monthly_report_title() -> None:
    html = build_report_html(
        period_type="monthly",
        since=_SINCE,
        until=_UNTIL,
        managers=[],
        heatmap_rows=[],
        event_rows=[],
    )
    assert "Monthly Risk Report" in html


# ---------------------------------------------------------------------------
# build_report_html — manager with no events
# ---------------------------------------------------------------------------


def test_manager_with_no_events_shows_clean() -> None:
    mgr = _mgr(tg_username="ivan_petrov")
    html = build_report_html(
        period_type="weekly",
        since=_SINCE,
        until=_UNTIL,
        managers=[mgr],
        heatmap_rows=[],
        event_rows=[],
    )
    assert "@ivan_petrov" in html
    assert "sb-clean" in html   # green checkmark in sidebar
    assert "no-events" in html


def test_manager_nav_attributes_present() -> None:
    # Navigation is client-side: the section carries data-mgr and the sidebar +
    # heat-map cell carry data-view (both keyed on the manager id) so the nav
    # script can switch to that manager's single view. No #mgr- anchors / ids.
    mgr = _mgr("Alice")
    html = build_report_html(
        period_type="weekly",
        since=_SINCE,
        until=_UNTIL,
        managers=[mgr],
        heatmap_rows=[],
        event_rows=[],
    )
    mid_str = str(mgr["id"])
    assert f'data-mgr="{mid_str}"' in html
    assert f'data-view="{mid_str}"' in html
    # The "All" default view and the roster search box are present.
    assert 'data-view="all"' in html
    assert "data-search" in html


# ---------------------------------------------------------------------------
# build_report_html — manager with events
# ---------------------------------------------------------------------------


def test_event_card_critical_class() -> None:
    mgr = _mgr()
    row = _event_row(mgr["id"], risk_level="critical", score=88)
    html = build_report_html(
        period_type="weekly",
        since=_SINCE,
        until=_UNTIL,
        managers=[mgr],
        heatmap_rows=[_heatmap_row(mgr["id"])],
        event_rows=[row],
    )
    assert 'class="event critical"' in html
    assert 'class="badge critical"' in html


def test_event_card_shows_partner_and_phrase() -> None:
    mgr = _mgr()
    row = _event_row(mgr["id"], partner_name="Globex")
    html = build_report_html(
        period_type="weekly",
        since=_SINCE,
        until=_UNTIL,
        managers=[mgr],
        heatmap_rows=[_heatmap_row(mgr["id"])],
        event_rows=[row],
    )
    assert "Globex" in html
    assert "между нами" in html
    assert "Off-the-books deal suspected." in html


def test_report_has_pdf_print_button() -> None:
    mgr = _mgr()
    html = build_report_html(
        period_type="weekly",
        since=_SINCE,
        until=_UNTIL,
        managers=[mgr],
        heatmap_rows=[_heatmap_row(mgr["id"])],
        event_rows=[_event_row(mgr["id"])],
    )
    assert 'class="print-btn"' in html
    assert "window.print()" in html
    assert "@media print" in html


def test_event_card_shows_author_and_role() -> None:
    mgr = _mgr()
    row = _event_row(mgr["id"], author_name="Иван Петров", author_role="internal")
    html = build_report_html(
        period_type="weekly",
        since=_SINCE,
        until=_UNTIL,
        managers=[mgr],
        heatmap_rows=[_heatmap_row(mgr["id"])],
        event_rows=[row],
    )
    assert "Иван Петров" in html
    assert '<div class="ev-author">' in html
    assert "сотрудник" in html  # internal role localised


def test_event_card_omits_author_block_when_unknown() -> None:
    mgr = _mgr()
    row = _event_row(mgr["id"], author_name=None, author_role=None)
    html = build_report_html(
        period_type="weekly",
        since=_SINCE,
        until=_UNTIL,
        managers=[mgr],
        heatmap_rows=[_heatmap_row(mgr["id"])],
        event_rows=[row],
    )
    assert '<div class="ev-author">' not in html


def test_event_risk_type_label_humanised() -> None:
    mgr = _mgr()
    row = _event_row(mgr["id"], risk_type="private_channel")
    html = build_report_html(
        period_type="weekly",
        since=_SINCE,
        until=_UNTIL,
        managers=[mgr],
        heatmap_rows=[_heatmap_row(mgr["id"], risk_type="private_channel")],
        event_rows=[row],
    )
    assert "Private Channel" in html


def test_toc_shows_critical_count() -> None:
    mgr = _mgr("Bob")
    rows = [_event_row(mgr["id"], risk_level="critical") for _ in range(2)]
    html = build_report_html(
        period_type="weekly",
        since=_SINCE,
        until=_UNTIL,
        managers=[mgr],
        heatmap_rows=[_heatmap_row(mgr["id"], cnt=2)],
        event_rows=rows,
    )
    assert "sb-crit" in html   # critical pill in sidebar
    assert "Bob" in html


# ---------------------------------------------------------------------------
# build_report_html — All-view summary (category bars, manager cards, clean)
# ---------------------------------------------------------------------------


def test_all_view_shows_risk_by_category() -> None:
    # The wide matrix is replaced by a compact per-category bar list.
    mgr = _mgr()
    html = build_report_html(
        period_type="weekly",
        since=_SINCE,
        until=_UNTIL,
        managers=[mgr],
        heatmap_rows=[],
        event_rows=[_event_row(mgr["id"], risk_type="private_channel")],
    )
    assert "Risk by Category" in html
    assert 'class="cat-row"' in html
    assert "Private Channel" in html


def test_all_view_manager_card_shows_severity() -> None:
    mgr = _mgr("Alice")
    rows = [
        _event_row(mgr["id"], risk_level="critical"),
        _event_row(mgr["id"], risk_level="high"),
        _event_row(mgr["id"], risk_level="medium"),
    ]
    html = build_report_html(
        period_type="weekly",
        since=_SINCE,
        until=_UNTIL,
        managers=[mgr],
        heatmap_rows=[],
        event_rows=rows,
    )
    assert "mgr-card" in html
    assert "mc-pill" in html
    assert "1 crit" in html
    assert "1 high" in html
    assert "1 med" in html


def test_all_view_clean_banner_when_no_events() -> None:
    mgr = _mgr()
    html = build_report_html(
        period_type="weekly",
        since=_SINCE,
        until=_UNTIL,
        managers=[mgr],
        heatmap_rows=[],
        event_rows=[],
    )
    assert "portfolio-clean" in html
    assert "No risk signals this period" in html
    # No category bars / manager cards when the portfolio is clean.
    assert 'class="cat-row"' not in html


def test_proposals_count_surfaced_in_summary() -> None:
    mgr = _mgr()
    html = build_report_html(
        period_type="weekly",
        since=_SINCE,
        until=_UNTIL,
        managers=[mgr],
        heatmap_rows=[],
        event_rows=[],
        proposals_count=7,
    )
    assert "Mgr proposals" in html
    assert ">7<" in html


def test_manager_slug_deep_link_present() -> None:
    # A manager with an aff_id gets a URL-safe slug for the #hash deep-link.
    mgr = _mgr(tg_username="anna_k", aff_id="78516")
    html = build_report_html(
        period_type="weekly",
        since=_SINCE,
        until=_UNTIL,
        managers=[mgr],
        heatmap_rows=[],
        event_rows=[_event_row(mgr["id"])],
    )
    assert 'data-slug="78516"' in html


# ---------------------------------------------------------------------------
# build_report_html — XSS escaping
# ---------------------------------------------------------------------------


def test_detected_phrase_is_html_escaped() -> None:
    mgr = _mgr()
    row = _event_row(mgr["id"])
    row["detected_phrase"] = "<script>alert(1)</script>"
    html = build_report_html(
        period_type="weekly",
        since=_SINCE,
        until=_UNTIL,
        managers=[mgr],
        heatmap_rows=[_heatmap_row(mgr["id"])],
        event_rows=[row],
    )
    # The dangerous payload must be escaped, not rendered live. (A generic
    # "<script>" check would false-positive on the page's own nav <script>.)
    assert "<script>alert(1)</script>" not in html
    assert "&lt;script&gt;alert(1)&lt;/script&gt;" in html


# ---------------------------------------------------------------------------
# Monthly date-range filter — picker + data island (weekly untouched)
# ---------------------------------------------------------------------------


def test_monthly_report_has_date_range_picker() -> None:
    mgr = _mgr()
    html = build_report_html(
        period_type="monthly",
        since=_SINCE,
        until=_UNTIL,
        managers=[mgr],
        heatmap_rows=[],
        event_rows=[_event_row(mgr["id"])],
    )
    # Native bounded date inputs (dropdown calendar) + reset.
    assert "data-daterange" in html
    assert "data-range-from" in html
    assert "data-range-to" in html
    assert "data-range-reset" in html
    # Data island the client filter reads from, bounded to the report window.
    assert "data-month-data" in html
    assert '"periodStart"' in html
    assert '"periodEnd"' in html


def test_monthly_data_island_carries_event_dates() -> None:
    mgr = _mgr()
    row = _event_row(mgr["id"], risk_level="high", risk_type="shadow_deal")
    html = build_report_html(
        period_type="monthly",
        since=_SINCE,
        until=_UNTIL,
        managers=[mgr],
        heatmap_rows=[],
        event_rows=[row],
    )
    # Event cards carry their date; the island carries the machine-readable copy.
    assert 'data-date="2026-06-05"' in html
    assert '"d": "2026-06-05"' in html
    assert '"lvl": "high"' in html


def test_monthly_proposal_dates_embedded() -> None:
    mgr = _mgr()
    html = build_report_html(
        period_type="monthly",
        since=_SINCE,
        until=_UNTIL,
        managers=[mgr],
        heatmap_rows=[],
        event_rows=[],
        proposal_dates=[datetime(2026, 6, 8, 10, 0, tzinfo=UTC)],
    )
    assert '"proposalDates"' in html
    assert '"2026-06-08"' in html


def test_weekly_report_has_no_date_range_filter() -> None:
    # Weekly is untouched: no picker, no data island, no month filter script.
    mgr = _mgr()
    html = build_report_html(
        period_type="weekly",
        since=_SINCE,
        until=_UNTIL,
        managers=[mgr],
        heatmap_rows=[],
        event_rows=[_event_row(mgr["id"])],
    )
    assert "data-daterange" not in html
    assert "data-month-data" not in html
    assert "data-month-ready" not in html


# ---------------------------------------------------------------------------
# RISK_CATEGORIES completeness
# ---------------------------------------------------------------------------


def test_risk_categories_has_data_leak() -> None:
    keys = [k for k, _ in RISK_CATEGORIES]
    assert "data_leak" in keys


def test_risk_categories_count() -> None:
    assert len(RISK_CATEGORIES) == 13


# ---------------------------------------------------------------------------
# generator.generate_report (monkeypatched)
# ---------------------------------------------------------------------------


class _NullConn:
    async def __aenter__(self) -> None:
        return None

    async def __aexit__(self, *exc: object) -> bool:
        return False


@pytest.fixture
def patched_generator(monkeypatch: pytest.MonkeyPatch) -> dict[str, Any]:
    """Patch all I/O in generator; return call recorder."""
    rec: dict[str, Any] = {
        "saved_html": None,
        "delivered": [],
        "slack_posts": [],
        "dashboard_pw": None,
        "slack_set": [],
        "revoked_keep": [],
        "superseded": [],
        "prev_dash": None,
    }

    mgr_id = uuid4()

    async def fake_managers(conn: Any) -> list[dict[str, Any]]:
        return [{"id": mgr_id, "full_name": "Test Mgr", "tg_username": "test_mgr", "aff_id": None}]

    async def fake_heatmap(conn: Any, since: Any, until: Any) -> list[dict[str, Any]]:
        return []

    async def fake_events(conn: Any, since: Any, until: Any) -> list[dict[str, Any]]:
        return []

    async def fake_proposals(conn: Any, since: Any, until: Any) -> int:
        return 0

    async def fake_proposal_dates(conn: Any, since: Any, until: Any) -> list[Any]:
        return []

    async def fake_save(conn: Any, *, period_type: Any, period_start: Any,
                        period_end: Any, rendered_html: Any, event_count: Any,
                        share_token: Any, expires_at: Any,
                        access_password: Any) -> Any:
        rec["saved_html"] = rendered_html
        return uuid4()

    async def fake_deliver(conn: Any, summary_id: Any) -> None:
        rec["delivered"].append(summary_id)

    async def fake_post(period_type: Any, since: Any, until: Any,
                        event_count: Any, dashboard_url: Any,
                        password: Any) -> str:
        rec["slack_posts"].append(dashboard_url)
        return "1700000000.000100"

    async def fake_create_dashboard(
        conn: Any, *, share_token: Any, access_password: Any, expires_at: Any
    ) -> Any:
        rec["dashboard_pw"] = access_password
        return uuid4()

    async def fake_get_active_dashboard(conn: Any) -> Any:
        return rec["prev_dash"]

    async def fake_set_dashboard_slack(
        conn: Any, dashboard_id: Any, slack_channel: Any, slack_ts: Any
    ) -> None:
        rec["slack_set"].append((slack_channel, slack_ts))

    async def fake_revoke(conn: Any, keep_id: Any) -> int:
        rec["revoked_keep"].append(keep_id)
        return 1

    async def fake_supersede(channel: Any, ts: Any) -> None:
        rec["superseded"].append((channel, ts))

    monkeypatch.setattr(gen_mod, "list_active_managers", fake_managers)
    monkeypatch.setattr(gen_mod, "risk_heatmap", fake_heatmap)
    monkeypatch.setattr(gen_mod, "list_events_for_report", fake_events)
    monkeypatch.setattr(gen_mod, "count_proposals", fake_proposals)
    monkeypatch.setattr(gen_mod, "list_proposal_dates", fake_proposal_dates)
    monkeypatch.setattr(gen_mod, "save_summary", fake_save)
    monkeypatch.setattr(gen_mod, "mark_summary_delivered", fake_deliver)
    monkeypatch.setattr(gen_mod, "_post_slack_link", fake_post)
    monkeypatch.setattr(gen_mod, "create_dashboard", fake_create_dashboard)
    monkeypatch.setattr(gen_mod, "get_active_dashboard", fake_get_active_dashboard)
    monkeypatch.setattr(gen_mod, "set_dashboard_slack", fake_set_dashboard_slack)
    monkeypatch.setattr(gen_mod, "revoke_dashboards_except", fake_revoke)
    monkeypatch.setattr(gen_mod, "_supersede_message", fake_supersede)
    monkeypatch.setattr(gen_mod, "acquire_connection", lambda: _NullConn())

    return rec


async def test_generate_report_saves_html_and_returns_url(
    patched_generator: dict[str, Any],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(gen_mod.settings, "SERVER_BASE_URL", "https://bot.example.com")
    result = await gen_mod.generate_report(period_type="weekly")
    assert result.url.startswith("https://bot.example.com/dashboard/")
    assert len(result.url.split("/dashboard/")[1]) == 64  # 32-byte hex token
    assert result.slack_delivered is True
    assert result.slack_error is None
    assert patched_generator["saved_html"] is not None
    assert "Weekly Risk Report" in patched_generator["saved_html"]
    assert len(patched_generator["delivered"]) == 1
    # password is surfaced
    assert result.dashboard_password is not None
    assert len(result.dashboard_password) == 8


async def test_generate_report_revokes_old_links(
    patched_generator: dict[str, Any],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # A new report records its Slack ts and revokes every other dashboard token,
    # keeping only the just-created one active.
    monkeypatch.setattr(gen_mod.settings, "SERVER_BASE_URL", "https://example.com")
    await gen_mod.generate_report(period_type="weekly")
    assert patched_generator["slack_set"], "new dashboard's Slack ts must be stored"
    channel, ts = patched_generator["slack_set"][0]
    assert ts == "1700000000.000100"
    assert len(patched_generator["revoked_keep"]) == 1  # revoke-all-except-new ran
    # No previous message → nothing superseded.
    assert patched_generator["superseded"] == []


async def test_generate_report_supersedes_previous_message(
    patched_generator: dict[str, Any],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # With a previously-advertised dashboard, its Slack message is retired.
    patched_generator["prev_dash"] = {
        "id": uuid4(),
        "slack_channel": "C123",
        "slack_ts": "1699999999.000001",
    }
    monkeypatch.setattr(gen_mod.settings, "SERVER_BASE_URL", "https://example.com")
    await gen_mod.generate_report(period_type="weekly")
    assert patched_generator["superseded"] == [("C123", "1699999999.000001")]


async def test_generate_report_slack_down_keeps_old_link(
    patched_generator: dict[str, Any],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # If the new post fails, the old link must NOT be revoked (avoid a channel
    # with zero working links).
    async def bad_post(*args: Any, **kwargs: Any) -> str:
        raise RuntimeError("Slack down")

    patched_generator["prev_dash"] = {
        "id": uuid4(),
        "slack_channel": "C123",
        "slack_ts": "1699999999.000001",
    }
    monkeypatch.setattr(gen_mod, "_post_slack_link", bad_post)
    monkeypatch.setattr(gen_mod.settings, "SERVER_BASE_URL", "https://example.com")
    result = await gen_mod.generate_report(period_type="weekly")
    assert result.slack_delivered is False
    assert patched_generator["revoked_keep"] == []   # nothing revoked
    assert patched_generator["superseded"] == []     # old message untouched


async def test_generate_report_monthly_url(
    patched_generator: dict[str, Any],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(gen_mod.settings, "SERVER_BASE_URL", "https://example.com")
    result = await gen_mod.generate_report(period_type="monthly")
    assert result.url.startswith("https://example.com/dashboard/")


async def test_generate_report_slack_failure_does_not_raise(
    patched_generator: dict[str, Any],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def bad_post(*args: Any, **kwargs: Any) -> None:
        raise RuntimeError("Slack down")

    monkeypatch.setattr(gen_mod, "_post_slack_link", bad_post)
    monkeypatch.setattr(gen_mod.settings, "SERVER_BASE_URL", "https://example.com")
    monkeypatch.setattr(
        gen_mod.settings.SUMMARY_ACCESS_TOKEN, "get_secret_value", lambda: "abc"
    )
    # Should not raise despite Slack failure — but must report it.
    result = await gen_mod.generate_report(period_type="weekly")
    assert result.url  # still returns URL
    assert result.slack_delivered is False
    assert result.slack_error == "Slack down"
    # Report is still persisted and marked delivered even when Slack is down.
    assert len(patched_generator["delivered"]) == 1


# ---------------------------------------------------------------------------
# _gen_password
# ---------------------------------------------------------------------------


def test_gen_password_format() -> None:
    from src.summary.generator import _gen_password

    pw = _gen_password()
    assert len(pw) == 8
    assert pw == pw.upper()
    for ch in pw:
        assert ch not in "01ILO"


# ---------------------------------------------------------------------------
# build_dashboard_html
# ---------------------------------------------------------------------------


def test_build_dashboard_html_shows_both_tabs() -> None:
    from src.summary.builder import build_dashboard_html

    weekly = build_report_html(
        period_type="weekly",
        since=_SINCE,
        until=_UNTIL,
        managers=[],
        heatmap_rows=[],
        event_rows=[],
    )
    monthly = build_report_html(
        period_type="monthly",
        since=_SINCE,
        until=_UNTIL,
        managers=[],
        heatmap_rows=[],
        event_rows=[],
    )
    html = build_dashboard_html(weekly_html=weekly, monthly_html=monthly)
    assert "tab-btn" in html
    assert "Weekly" in html
    assert "Monthly" in html
    assert "panel-weekly" in html
    assert "panel-monthly" in html


def test_build_dashboard_html_handles_missing_reports() -> None:
    from src.summary.builder import build_dashboard_html

    html = build_dashboard_html(weekly_html=None, monthly_html=None)
    assert "tab-empty" in html
    assert "Weekly" in html
    assert "Monthly" in html
