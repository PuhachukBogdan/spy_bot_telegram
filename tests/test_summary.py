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


def test_manager_anchor_id_present() -> None:
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
    assert f'id="mgr-{mid_str}"' in html
    assert f'href="#mgr-{mid_str}"' in html


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
# build_report_html — heatmap cell classes
# ---------------------------------------------------------------------------


def test_heatmap_zero_cell() -> None:
    mgr = _mgr()
    html = build_report_html(
        period_type="weekly",
        since=_SINCE,
        until=_UNTIL,
        managers=[mgr],
        heatmap_rows=[],
        event_rows=[],
    )
    assert "cell-zero" in html


def test_heatmap_warm_cell_one_event() -> None:
    mgr = _mgr()
    html = build_report_html(
        period_type="weekly",
        since=_SINCE,
        until=_UNTIL,
        managers=[mgr],
        heatmap_rows=[_heatmap_row(mgr["id"], cnt=1)],
        event_rows=[_event_row(mgr["id"])],
    )
    assert "cell-warm" in html


def test_heatmap_hot_cell_three_events() -> None:
    mgr = _mgr()
    html = build_report_html(
        period_type="weekly",
        since=_SINCE,
        until=_UNTIL,
        managers=[mgr],
        heatmap_rows=[_heatmap_row(mgr["id"], cnt=3)],
        event_rows=[_event_row(mgr["id"]) for _ in range(3)],
    )
    assert "cell-hot" in html


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
    assert "<script>" not in html
    assert "&lt;script&gt;" in html


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
    }

    mgr_id = uuid4()

    async def fake_managers(conn: Any) -> list[dict[str, Any]]:
        return [{"id": mgr_id, "full_name": "Test Mgr", "tg_username": "test_mgr", "aff_id": None}]

    async def fake_heatmap(conn: Any, since: Any, until: Any) -> list[dict[str, Any]]:
        return []

    async def fake_events(conn: Any, since: Any, until: Any) -> list[dict[str, Any]]:
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
                        password: Any) -> None:
        rec["slack_posts"].append(dashboard_url)

    async def fake_create_dashboard(
        conn: Any, *, share_token: Any, access_password: Any, expires_at: Any
    ) -> Any:
        rec["dashboard_pw"] = access_password
        return uuid4()

    monkeypatch.setattr(gen_mod, "list_active_managers", fake_managers)
    monkeypatch.setattr(gen_mod, "risk_heatmap", fake_heatmap)
    monkeypatch.setattr(gen_mod, "list_events_for_report", fake_events)
    monkeypatch.setattr(gen_mod, "save_summary", fake_save)
    monkeypatch.setattr(gen_mod, "mark_summary_delivered", fake_deliver)
    monkeypatch.setattr(gen_mod, "_post_slack_link", fake_post)
    monkeypatch.setattr(gen_mod, "create_dashboard", fake_create_dashboard)
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
