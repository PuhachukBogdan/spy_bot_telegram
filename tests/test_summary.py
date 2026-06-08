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
    _risk_type_label,
    build_report_html,
)

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

_SINCE = datetime(2026, 6, 2, tzinfo=UTC)
_UNTIL = datetime(2026, 6, 9, tzinfo=UTC)


def _mgr(full_name: str = "Test Manager") -> dict[str, Any]:
    return {"id": uuid4(), "full_name": full_name}


def _event_row(
    manager_id: Any,
    risk_level: str = "high",
    risk_type: str = "shadow_deal",
    partner_name: str = "Acme",
    score: int = 72,
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
    mgr = _mgr("Ivan Petrov")
    html = build_report_html(
        period_type="weekly",
        since=_SINCE,
        until=_UNTIL,
        managers=[mgr],
        heatmap_rows=[],
        event_rows=[],
    )
    assert "Ivan Petrov" in html
    assert "чисто" in html
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
    assert "crit-count" in html
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
    }

    mgr_id = uuid4()

    async def fake_managers(conn: Any) -> list[dict[str, Any]]:
        return [{"id": mgr_id, "full_name": "Test Mgr"}]

    async def fake_heatmap(conn: Any, since: Any, until: Any) -> list[dict[str, Any]]:
        return []

    async def fake_events(conn: Any, since: Any, until: Any) -> list[dict[str, Any]]:
        return []

    async def fake_save(conn: Any, *, period_type: Any, period_start: Any,
                        period_end: Any, rendered_html: Any, event_count: Any) -> Any:
        rec["saved_html"] = rendered_html
        return uuid4()

    async def fake_deliver(conn: Any, summary_id: Any) -> None:
        rec["delivered"].append(summary_id)

    async def fake_post(period_type: Any, since: Any, until: Any,
                        event_count: Any, report_url: Any) -> None:
        rec["slack_posts"].append(report_url)

    monkeypatch.setattr(gen_mod, "list_active_managers", fake_managers)
    monkeypatch.setattr(gen_mod, "risk_heatmap", fake_heatmap)
    monkeypatch.setattr(gen_mod, "list_events_for_report", fake_events)
    monkeypatch.setattr(gen_mod, "save_summary", fake_save)
    monkeypatch.setattr(gen_mod, "mark_summary_delivered", fake_deliver)
    monkeypatch.setattr(gen_mod, "_post_slack_link", fake_post)
    monkeypatch.setattr(gen_mod, "acquire_connection", lambda: _NullConn())

    return rec


async def test_generate_report_saves_html_and_returns_url(
    patched_generator: dict[str, Any],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(gen_mod.settings, "SERVER_BASE_URL", "https://bot.example.com")
    monkeypatch.setattr(
        gen_mod.settings.SUMMARY_ACCESS_TOKEN, "get_secret_value", lambda: "tok123"
    )
    url = await gen_mod.generate_report(period_type="weekly")
    assert url.startswith("https://bot.example.com/reports/weekly/")
    assert "tok123" in url
    assert patched_generator["saved_html"] is not None
    assert "Weekly Risk Report" in patched_generator["saved_html"]
    assert len(patched_generator["delivered"]) == 1


async def test_generate_report_monthly_url(
    patched_generator: dict[str, Any],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(gen_mod.settings, "SERVER_BASE_URL", "https://example.com")
    monkeypatch.setattr(
        gen_mod.settings.SUMMARY_ACCESS_TOKEN, "get_secret_value", lambda: "abc"
    )
    url = await gen_mod.generate_report(period_type="monthly")
    assert "/reports/monthly/" in url


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
    # Should not raise despite Slack failure
    url = await gen_mod.generate_report(period_type="weekly")
    assert url  # still returns URL
