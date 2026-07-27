"""Tests for the HTML report builder and generator. Phase 16 (chat-centric).

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
    _chat_name,
    _chat_slug,
    _risk_type_label,
    build_report_html,
)

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

_SINCE = datetime(2026, 6, 2, tzinfo=UTC)
_UNTIL = datetime(2026, 6, 9, tzinfo=UTC)


def _chat(
    chat_name: str = "77777 | Acme | Beton.Win",
    manager_name: str = "Kowalski",
    topic_name: str | None = None,
    is_test: bool = False,
    manager_is_test: bool = False,
) -> dict[str, Any]:
    return {
        "id": uuid4(),
        "chat_name": chat_name,
        "topic_name": topic_name,
        "manager_name": manager_name,
        "is_test": is_test,
        "manager_is_test": manager_is_test,
    }


def _event_row(
    chat_id: Any,
    risk_level: str = "high",
    risk_type: str = "shadow_deal",
    partner_name: str = "Acme",
    score: int = 72,
    author_name: str | None = "Иван Петров",
    author_role: str | None = "internal",
) -> dict[str, Any]:
    return {
        "id": uuid4(),
        "chat_id": chat_id,
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


# ---------------------------------------------------------------------------
# _risk_type_label
# ---------------------------------------------------------------------------


def test_known_risk_type_label() -> None:
    assert _risk_type_label("shadow_deal") == "Shadow Deal"
    assert _risk_type_label("data_leak") == "Data Leak"


def test_unknown_risk_type_falls_back_to_title() -> None:
    assert _risk_type_label("some_new_type") == "Some New Type"


# ---------------------------------------------------------------------------
# _chat_name / _chat_slug
# ---------------------------------------------------------------------------


def test_chat_name_plain() -> None:
    assert _chat_name({"chat_name": "78516 | Acme | Beton.Win"}) == "78516 | Acme | Beton.Win"


def test_chat_name_with_topic_appended() -> None:
    name = _chat_name({"chat_name": "78516 | Acme", "topic_name": "Payments"})
    assert name == "78516 | Acme / Payments"


def test_chat_name_falls_back_when_untitled() -> None:
    # No title at all still yields a stable, non-empty label (never a bare "").
    name = _chat_name({"id": "abcdef12-0000-0000-0000-000000000000"})
    assert name.startswith("chat ")


def test_chat_slug_is_url_safe_and_deduped() -> None:
    taken: set[str] = set()
    s1 = _chat_slug({"chat_name": "78516 | Acme"}, taken)
    s2 = _chat_slug({"chat_name": "78516 | Acme"}, taken)  # collision
    assert s1 == "78516-acme"
    assert s2 == "78516-acme-2"


# ---------------------------------------------------------------------------
# build_report_html — empty
# ---------------------------------------------------------------------------


def test_build_empty_report_is_valid_html() -> None:
    html = build_report_html(
        period_type="weekly",
        since=_SINCE,
        until=_UNTIL,
        chats=[],
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
        chats=[],
        event_rows=[],
    )
    assert "Monthly Risk Report" in html


# ---------------------------------------------------------------------------
# build_report_html — roster label + counter say "Chats", not "Managers"
# ---------------------------------------------------------------------------


def test_roster_labelled_chats_with_count() -> None:
    chats = [_chat(chat_name="A | Beton.Win"), _chat(chat_name="B | Beton.Win")]
    html = build_report_html(
        period_type="weekly", since=_SINCE, until=_UNTIL, chats=chats, event_rows=[]
    )
    # The sidebar section label carries the live chat count…
    assert "Chats · 2" in html
    # …and the top stat cell is "Chats flagged", not "Managers flagged".
    assert "Chats flagged" in html
    assert "Managers flagged" not in html


# ---------------------------------------------------------------------------
# build_report_html — chat with no events
# ---------------------------------------------------------------------------


def test_chat_with_no_events_shows_clean() -> None:
    chat = _chat(chat_name="88888 | Quiet | Beton.Win")
    html = build_report_html(
        period_type="weekly",
        since=_SINCE,
        until=_UNTIL,
        chats=[chat],
        event_rows=[],
    )
    assert "88888 | Quiet | Beton.Win" in html
    assert "sb-clean" in html   # green checkmark in sidebar
    assert "no-events" in html


def test_chat_nav_attributes_present() -> None:
    # Navigation is client-side: the section carries data-mgr and the sidebar
    # carries data-view (both keyed on the CHAT id) so the nav script can switch
    # to that chat's single view.
    chat = _chat()
    html = build_report_html(
        period_type="weekly",
        since=_SINCE,
        until=_UNTIL,
        chats=[chat],
        event_rows=[],
    )
    cid_str = str(chat["id"])
    assert f'data-mgr="{cid_str}"' in html
    assert f'data-view="{cid_str}"' in html
    assert 'data-view="all"' in html
    assert "data-search" in html


def test_chat_card_and_dossier_show_manager_name() -> None:
    chat = _chat(chat_name="78516 | Acme | Beton.Win", manager_name="Christopher")
    html = build_report_html(
        period_type="weekly",
        since=_SINCE,
        until=_UNTIL,
        chats=[chat],
        event_rows=[_event_row(chat["id"])],
    )
    assert "Manager: Christopher" in html
    assert "mc-mgr" in html   # summary-card manager sub-line
    assert "mgr-sub" in html  # dossier manager subtitle


def test_manager_search_corpus_includes_manager_name() -> None:
    # The roster is searchable by manager name via data-filter (not data-name,
    # which stays the visible chat label so the monthly re-render can't corrupt it).
    chat = _chat(chat_name="78516 | Acme | Beton.Win", manager_name="Christopher")
    html = build_report_html(
        period_type="weekly", since=_SINCE, until=_UNTIL, chats=[chat], event_rows=[]
    )
    assert 'data-filter="78516 | Acme | Beton.Win Christopher"' in html


# ---------------------------------------------------------------------------
# build_report_html — chat with events
# ---------------------------------------------------------------------------


def test_event_card_critical_class() -> None:
    chat = _chat()
    row = _event_row(chat["id"], risk_level="critical", score=88)
    html = build_report_html(
        period_type="weekly",
        since=_SINCE,
        until=_UNTIL,
        chats=[chat],
        event_rows=[row],
    )
    assert 'class="event critical"' in html
    assert 'class="badge critical"' in html


def test_event_card_shows_partner_and_phrase() -> None:
    chat = _chat()
    row = _event_row(chat["id"], partner_name="Globex")
    html = build_report_html(
        period_type="weekly",
        since=_SINCE,
        until=_UNTIL,
        chats=[chat],
        event_rows=[row],
    )
    assert "Globex" in html
    assert "между нами" in html
    assert "Off-the-books deal suspected." in html


def test_report_has_pdf_print_button() -> None:
    chat = _chat()
    html = build_report_html(
        period_type="weekly",
        since=_SINCE,
        until=_UNTIL,
        chats=[chat],
        event_rows=[_event_row(chat["id"])],
    )
    assert 'class="print-btn"' in html
    assert "window.print()" in html
    assert "@media print" in html


def test_event_card_shows_author_and_role() -> None:
    chat = _chat()
    row = _event_row(chat["id"], author_name="Иван Петров", author_role="internal")
    html = build_report_html(
        period_type="weekly",
        since=_SINCE,
        until=_UNTIL,
        chats=[chat],
        event_rows=[row],
    )
    assert "Иван Петров" in html
    assert '<div class="ev-author">' in html
    assert "сотрудник" in html  # internal role localised


def test_event_card_omits_author_block_when_unknown() -> None:
    chat = _chat()
    row = _event_row(chat["id"], author_name=None, author_role=None)
    html = build_report_html(
        period_type="weekly",
        since=_SINCE,
        until=_UNTIL,
        chats=[chat],
        event_rows=[row],
    )
    assert '<div class="ev-author">' not in html


def test_event_risk_type_label_humanised() -> None:
    chat = _chat()
    row = _event_row(chat["id"], risk_type="private_channel")
    html = build_report_html(
        period_type="weekly",
        since=_SINCE,
        until=_UNTIL,
        chats=[chat],
        event_rows=[row],
    )
    assert "Private Channel" in html


def test_toc_shows_critical_count() -> None:
    chat = _chat(chat_name="Bob | Beton.Win")
    rows = [_event_row(chat["id"], risk_level="critical") for _ in range(2)]
    html = build_report_html(
        period_type="weekly",
        since=_SINCE,
        until=_UNTIL,
        chats=[chat],
        event_rows=rows,
    )
    assert "sb-crit" in html   # critical pill in sidebar
    assert "Bob | Beton.Win" in html


def test_events_without_partner_still_grouped_by_chat() -> None:
    # A NULL partner (partner_name falls back to chat title in the query) must not
    # drop the event — the old partner→owner JOIN would have lost it.
    chat = _chat(chat_name="99999 | Orphan | Beton.Win")
    row = _event_row(chat["id"], partner_name="99999 | Orphan | Beton.Win")
    html = build_report_html(
        period_type="weekly", since=_SINCE, until=_UNTIL, chats=[chat], event_rows=[row]
    )
    assert "1 event" in html


# ---------------------------------------------------------------------------
# build_report_html — All-view summary (category bars, chat cards, clean)
# ---------------------------------------------------------------------------


def test_all_view_shows_risk_by_category() -> None:
    chat = _chat()
    html = build_report_html(
        period_type="weekly",
        since=_SINCE,
        until=_UNTIL,
        chats=[chat],
        event_rows=[_event_row(chat["id"], risk_type="private_channel")],
    )
    assert "Risk by Category" in html
    assert 'class="cat-row"' in html
    assert "Private Channel" in html


def test_all_view_chat_card_shows_severity() -> None:
    chat = _chat(chat_name="Alice | Beton.Win")
    rows = [
        _event_row(chat["id"], risk_level="critical"),
        _event_row(chat["id"], risk_level="high"),
        _event_row(chat["id"], risk_level="medium"),
    ]
    html = build_report_html(
        period_type="weekly",
        since=_SINCE,
        until=_UNTIL,
        chats=[chat],
        event_rows=rows,
    )
    assert "mgr-card" in html
    assert "mc-pill" in html
    assert "1 crit" in html
    assert "1 high" in html
    assert "1 med" in html


def test_all_view_clean_banner_when_no_events() -> None:
    chat = _chat()
    html = build_report_html(
        period_type="weekly",
        since=_SINCE,
        until=_UNTIL,
        chats=[chat],
        event_rows=[],
    )
    assert "portfolio-clean" in html
    assert "No risk signals this period" in html
    assert 'class="cat-row"' not in html


def test_proposals_count_surfaced_in_summary() -> None:
    chat = _chat()
    html = build_report_html(
        period_type="weekly",
        since=_SINCE,
        until=_UNTIL,
        chats=[chat],
        event_rows=[],
        proposals_count=7,
    )
    assert "Mgr proposals" in html
    assert ">7<" in html


def test_new_chats_metric_surfaced() -> None:
    chat = _chat()
    html = build_report_html(
        period_type="weekly",
        since=_SINCE,
        until=_UNTIL,
        chats=[chat],
        event_rows=[],
        chats_added=5,
    )
    assert "New chats" in html
    assert 'data-stat="chatsadded">5<' in html


def test_monthly_chats_added_dates_embedded() -> None:
    chat = _chat()
    html = build_report_html(
        period_type="monthly",
        since=_SINCE,
        until=_UNTIL,
        chats=[chat],
        event_rows=[],
        chats_added=2,
        chats_added_dates=[datetime(2026, 6, 4, 9, 0, tzinfo=UTC)],
    )
    assert '"chatsAddedDates"' in html
    assert '"2026-06-04"' in html


def test_all_view_shows_only_risky_chats() -> None:
    # A clean chat is NOT rendered as a card (only risky ones); it still appears
    # in the sidebar roster.
    risky = _chat(chat_name="RISKY | Beton.Win")
    clean = _chat(chat_name="CLEAN | Beton.Win")
    html = build_report_html(
        period_type="weekly", since=_SINCE, until=_UNTIL,
        chats=[risky, clean], event_rows=[_event_row(risky["id"])],
    )
    assert "Chats with risk signals" in html
    # Exactly one card (the risky chat); the clean chat has no card…
    assert html.count('class="mgr-card"') == 1
    cards_div = html.split('<div class="mgr-cards">')[1].split("</div>")[0]
    assert "RISKY | Beton.Win" in cards_div
    assert "CLEAN | Beton.Win" not in cards_div
    # …but the clean chat is still in the sidebar roster.
    assert 'data-name="CLEAN | Beton.Win"' in html


def test_proposals_badge_on_chat() -> None:
    chat = _chat()
    html = build_report_html(
        period_type="weekly", since=_SINCE, until=_UNTIL,
        chats=[chat], event_rows=[_event_row(chat["id"])],
        proposals_by_chat={str(chat["id"]): 3},
    )
    assert "3 proposal" in html  # card/dossier badge
    assert "mc-prop" in html


def test_test_chat_carries_flag_and_filter_panel_present() -> None:
    chat = _chat(chat_name="Test bot group 123", is_test=True)
    html = build_report_html(
        period_type="weekly", since=_SINCE, until=_UNTIL,
        chats=[chat], event_rows=[],
    )
    assert 'data-test="1"' in html          # flag rendered for the filter JS
    assert "data-filter-panel" in html      # filter panel present
    assert "data-show-test-chats" in html   # test toggle present


def test_test_chat_excluded_from_headline_stats() -> None:
    real = _chat(chat_name="REAL | Beton.Win")
    test = _chat(chat_name="Test bot group 123", is_test=True)
    html = build_report_html(
        period_type="weekly", since=_SINCE, until=_UNTIL,
        chats=[real, test],
        event_rows=[_event_row(test["id"], risk_level="critical")],
    )
    # The test chat's critical event must NOT inflate the headline counters.
    assert "Chats · 1" in html                       # only the real chat counted
    assert 'data-stat="critical">0<' in html         # test crit excluded


def test_category_and_manager_filter_controls_present() -> None:
    chat = _chat()
    html = build_report_html(
        period_type="weekly", since=_SINCE, until=_UNTIL,
        chats=[chat], event_rows=[_event_row(chat["id"], risk_type="shadow_deal")],
    )
    assert "data-cat-filter" in html       # category checkboxes
    assert "data-mgr-filter" in html       # manager checkboxes
    assert 'data-cats="shadow_deal"' in html


def test_chat_slug_deep_link_present() -> None:
    # A chat title yields a URL-safe slug for the #hash deep-link.
    chat = _chat(chat_name="78516 | Acme")
    html = build_report_html(
        period_type="weekly",
        since=_SINCE,
        until=_UNTIL,
        chats=[chat],
        event_rows=[_event_row(chat["id"])],
    )
    assert 'data-slug="78516-acme"' in html


# ---------------------------------------------------------------------------
# build_report_html — XSS escaping
# ---------------------------------------------------------------------------


def test_detected_phrase_is_html_escaped() -> None:
    chat = _chat()
    row = _event_row(chat["id"])
    row["detected_phrase"] = "<script>alert(1)</script>"
    html = build_report_html(
        period_type="weekly",
        since=_SINCE,
        until=_UNTIL,
        chats=[chat],
        event_rows=[row],
    )
    assert "<script>alert(1)</script>" not in html
    assert "&lt;script&gt;alert(1)&lt;/script&gt;" in html


# ---------------------------------------------------------------------------
# Monthly date-range filter — picker + data island (weekly untouched)
# ---------------------------------------------------------------------------


def test_monthly_report_has_date_range_picker() -> None:
    chat = _chat()
    html = build_report_html(
        period_type="monthly",
        since=_SINCE,
        until=_UNTIL,
        chats=[chat],
        event_rows=[_event_row(chat["id"])],
    )
    assert "data-daterange" in html
    assert "data-range-from" in html
    assert "data-range-to" in html
    assert "data-range-reset" in html
    assert "data-month-data" in html
    assert '"periodStart"' in html
    assert '"periodEnd"' in html


def test_monthly_data_island_carries_event_dates() -> None:
    chat = _chat()
    row = _event_row(chat["id"], risk_level="high", risk_type="shadow_deal")
    html = build_report_html(
        period_type="monthly",
        since=_SINCE,
        until=_UNTIL,
        chats=[chat],
        event_rows=[row],
    )
    assert 'data-date="2026-06-05"' in html
    assert '"d": "2026-06-05"' in html
    assert '"lvl": "high"' in html


def test_monthly_data_island_carries_manager_name() -> None:
    chat = _chat(manager_name="Christopher")
    html = build_report_html(
        period_type="monthly",
        since=_SINCE,
        until=_UNTIL,
        chats=[chat],
        event_rows=[_event_row(chat["id"])],
    )
    # The client-side filter re-renders chat cards, so each unit carries its
    # manager name into the data island.
    assert '"managerName": "Christopher"' in html


def test_monthly_proposal_dates_embedded() -> None:
    chat = _chat()
    html = build_report_html(
        period_type="monthly",
        since=_SINCE,
        until=_UNTIL,
        chats=[chat],
        event_rows=[],
        proposal_dates=[datetime(2026, 6, 8, 10, 0, tzinfo=UTC)],
    )
    assert '"proposalDates"' in html
    assert '"2026-06-08"' in html


def test_weekly_report_has_no_date_range_filter() -> None:
    chat = _chat()
    html = build_report_html(
        period_type="weekly",
        since=_SINCE,
        until=_UNTIL,
        chats=[chat],
        event_rows=[_event_row(chat["id"])],
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

    chat_id = uuid4()

    async def fake_chats(conn: Any) -> list[dict[str, Any]]:
        return [
            {
                "id": chat_id,
                "chat_name": "77777 | Test | Beton.Win",
                "topic_name": None,
                "manager_name": "Kowalski",
            }
        ]

    async def fake_events(conn: Any, since: Any, until: Any) -> list[dict[str, Any]]:
        return []

    async def fake_chats_added(conn: Any, since: Any, until: Any) -> int:
        return 0

    async def fake_chat_added_dates(conn: Any, since: Any, until: Any) -> list[Any]:
        return []

    async def fake_proposals_by_chat(conn: Any, since: Any, until: Any) -> dict[str, int]:
        return {}

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

    monkeypatch.setattr(gen_mod, "list_active_chats", fake_chats)
    monkeypatch.setattr(gen_mod, "list_events_by_chat", fake_events)
    monkeypatch.setattr(gen_mod, "count_chats_added", fake_chats_added)
    monkeypatch.setattr(gen_mod, "list_chat_added_dates", fake_chat_added_dates)
    monkeypatch.setattr(gen_mod, "count_proposals_by_chat", fake_proposals_by_chat)
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
    assert result.dashboard_password is not None
    assert len(result.dashboard_password) == 8


async def test_generate_report_revokes_old_links(
    patched_generator: dict[str, Any],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(gen_mod.settings, "SERVER_BASE_URL", "https://example.com")
    await gen_mod.generate_report(period_type="weekly")
    assert patched_generator["slack_set"], "new dashboard's Slack ts must be stored"
    channel, ts = patched_generator["slack_set"][0]
    assert ts == "1700000000.000100"
    assert len(patched_generator["revoked_keep"]) == 1
    assert patched_generator["superseded"] == []


async def test_generate_report_supersedes_previous_message(
    patched_generator: dict[str, Any],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
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
    assert patched_generator["revoked_keep"] == []
    assert patched_generator["superseded"] == []


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
    result = await gen_mod.generate_report(period_type="weekly")
    assert result.url
    assert result.slack_delivered is False
    assert result.slack_error == "Slack down"
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
        period_type="weekly", since=_SINCE, until=_UNTIL, chats=[], event_rows=[]
    )
    monthly = build_report_html(
        period_type="monthly", since=_SINCE, until=_UNTIL, chats=[], event_rows=[]
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


def test_dashboard_has_daily_tab_first() -> None:
    from src.summary.builder import build_dashboard_html

    html = build_dashboard_html(weekly_html=None, monthly_html=None, daily_html="<p>hi</p>")
    assert "panel-daily" in html
    # Daily tab button appears before Weekly (first tab).
    assert html.index('data-tab="daily"') < html.index('data-tab="weekly"')


def test_build_daily_card_renders_metrics() -> None:
    from src.summary.builder import build_daily_card

    class _D:
        messages_total = 235
        significant = 227
        active_chats = 28
        total_active_chats = 159
        active_managers = 3
        risk_low = 0
        risk_medium = 0
        risk_high = 0
        risk_critical = 1
        new_chats = 0
        new_partners = 0
        active_chat_rows = [("80528 | DeepStake | Betonwin", 43), ("Other | BW", 5)]
        has_activity = True

    html = build_daily_card(
        day="2026-07-20", digest=_D(), min_day="2026-06-20",
        max_day="2026-07-21", generated_at="2026-07-21 12:00 UTC",
    )
    assert "Daily Digest" in html
    assert "235" in html
    assert "28/159" in html
    assert "1 critical" in html
    assert "Active chats · 2" in html
    assert "80528 | DeepStake | Betonwin" in html
    assert "dc-head" in html and "Message count" in html  # column headers
    assert 'name="day"' in html  # date picker
    assert 'onchange="this.form.submit()"' in html  # auto-load, no Go button
    assert ">Go<" not in html


def test_build_daily_card_no_activity() -> None:
    from src.summary.builder import build_daily_card

    class _D:
        messages_total = 0
        significant = 0
        active_chats = 0
        total_active_chats = 159
        active_managers = 0
        risk_low = risk_medium = risk_high = risk_critical = 0
        new_chats = new_partners = 0
        active_chat_rows: list[tuple[str, int]] = []
        has_activity = False

    html = build_daily_card(
        day="2026-07-19", digest=_D(), min_day="2026-06-19",
        max_day="2026-07-21", generated_at="x",
    )
    assert "No activity on 2026-07-19" in html


# ---------------------------------------------------------------------------
# Daily digest: hourly auto-refresh of the CURRENT day
# ---------------------------------------------------------------------------


class _LiveD:
    """Minimal digest stand-in for the auto-refresh marker tests."""

    messages_total = 12
    significant = 3
    active_chats = 2
    total_active_chats = 159
    active_managers = 1
    risk_low = risk_medium = risk_high = risk_critical = 0
    new_chats = new_partners = 0
    active_chat_rows: list[tuple[str, int]] = []
    has_activity = True


def test_daily_card_marks_current_day_live() -> None:
    from src.summary.builder import build_daily_card

    html = build_daily_card(
        day="2026-07-21", digest=_LiveD(), min_day="2026-06-21",
        max_day="2026-07-21", generated_at="2026-07-21 12:00 UTC",
    )
    assert "data-daily-live" in html
    assert "refreshes hourly" in html


def test_daily_card_past_day_is_not_live() -> None:
    from src.summary.builder import build_daily_card

    html = build_daily_card(
        day="2026-07-18", digest=_LiveD(), min_day="2026-06-21",
        max_day="2026-07-21", generated_at="2026-07-21 12:00 UTC",
    )
    assert "data-daily-live" not in html


def test_daily_card_live_marker_survives_empty_day() -> None:
    # A quiet current day takes the early "no activity" return — it must still
    # carry the marker, else the panel would stop refreshing until a reload.
    from src.summary.builder import build_daily_card

    class _Empty(_LiveD):
        messages_total = significant = 0
        has_activity = False

    html = build_daily_card(
        day="2026-07-21", digest=_Empty(), min_day="2026-06-21",
        max_day="2026-07-21", generated_at="x",
    )
    assert "No activity on 2026-07-21" in html
    assert "data-daily-live" in html


def test_dashboard_polls_daily_fragment_hourly() -> None:
    from src.summary.builder import build_dashboard_html

    html = build_dashboard_html(weekly_html=None, monthly_html=None, daily_html="<p>hi</p>")
    assert "/daily?day=today" in html  # fragment URL, always the current day
    assert "3600000" in html  # one-hour cadence
    assert "data-daily-live" in html  # gated on the live marker
    assert "location.reload" not in html  # swap in place, never a full reload
