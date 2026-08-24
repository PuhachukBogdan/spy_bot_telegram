"""Filling the React shell with the metrics island. No DB, no network."""

from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from uuid import uuid4

import pytest

from src.metrics.collect import ChatCoverage, ManagerMetrics
from src.metrics.preview import build_payload
from src.metrics.shell import fill_shell, load_shell, render_with_shell, shell_path
from src.metrics.sla import SlaOutcome, tally
from src.metrics.window import resolve_metrics_window

SHELL = "<!doctype html><html><body><div id='root'></div></body></html>"


def _payload() -> dict[str, Any]:
    window = resolve_metrics_window(
        datetime(2026, 8, 1, tzinfo=UTC), datetime(2026, 8, 31, tzinfo=UTC), epoch=None
    )
    metrics = [
        ManagerMetrics(
            manager_id=uuid4(),
            name="Mirror | Betonwin",
            coverage=ChatCoverage(total=57, active=37),
            sla=tally([SlaOutcome.MET, SlaOutcome.MISSED, SlaOutcome.OFFLINE]),
            proposals=46,
        )
    ]
    return build_payload(metrics, window)


def test_island_is_injected_before_body_close() -> None:
    filled = fill_shell(SHELL, {"a": 1})
    assert filled.count('id="report-data"') == 1
    assert filled.index("report-data") < filled.index("</body>")
    assert filled.startswith("<!doctype html>")


def test_island_parses_back_to_the_payload() -> None:
    payload = _payload()
    filled = fill_shell(SHELL, payload)
    raw = filled.split('type="application/json">')[1].split("</script>")[0]
    assert json.loads(raw.replace("\\u003c", "<")) == json.loads(json.dumps(payload))


def test_script_close_in_data_cannot_break_out() -> None:
    # A manager literally named "</script><img>" must not end the element early.
    filled = fill_shell(SHELL, {"name": "</script><img src=x>"})
    assert "</script><img" not in filled
    assert "\\u003c/script" in filled
    # Exactly one real </script> — the one that closes our island.
    assert filled.count("</script>") == 1


def test_line_separators_are_escaped() -> None:
    filled = fill_shell(SHELL, {"t": "a\u2028b\u2029c"})
    assert "\\u2028" in filled and "\\u2029" in filled


def test_shell_without_body_still_yields_a_page() -> None:
    filled = fill_shell("<html><div id='root'></div></html>", {"a": 1})
    assert 'id="report-data"' in filled


def test_cyrillic_is_not_escaped_to_ascii() -> None:
    filled = fill_shell(SHELL, {"name": "Гералт"})
    assert "Гералт" in filled


# ---------------------------------------------------------------------------
# payload shape — must match frontend/src/data.ts
# ---------------------------------------------------------------------------


def test_payload_shape() -> None:
    payload = _payload()
    assert set(payload) == {
        "generatedAt", "since", "until", "previous", "epoch", "thresholds",
        "categories", "managers", "trends",
    }
    manager = payload["managers"][0]
    assert set(manager) == {
        "id", "name", "slaPercent", "slaMet", "slaRated", "slaOffline",
        "coveragePercent", "chatsActive", "chatsTotal", "proposals", "workHours",
        "risksOwn", "risksContext", "chats", "risks",
    }
    assert manager["slaRated"] == 2  # offline excluded
    assert manager["slaOffline"] == 1
    assert manager["coveragePercent"] == 64.9


def test_no_data_stays_null_not_zero() -> None:
    payload = build_payload(
        [ManagerMetrics(manager_id=uuid4(), name="Quiet")],
        resolve_metrics_window(
            datetime(2026, 8, 1, tzinfo=UTC),
            datetime(2026, 8, 2, tzinfo=UTC),
            epoch=None,
        ),
    )
    manager = payload["managers"][0]
    assert manager["slaPercent"] is None
    assert manager["coveragePercent"] is None
    assert manager["workHours"] is None


def test_payload_survives_json_roundtrip() -> None:
    # The island is JSON; anything unserialisable here is a blank page in prod.
    assert json.loads(json.dumps(_payload(), default=str))


def test_chat_days_entries_carry_the_chat_id() -> None:
    # The dossier's chat table joins these entries to the named chat rows by id
    # to recount messages for the selected period — without `i` the client
    # could only count anonymously.
    from zoneinfo import ZoneInfo

    from src.metrics.preview import _chat_days_payload

    chat_id = uuid4()
    manager_id = uuid4()
    registry = [
        {
            "chat_id": chat_id,
            "manager_id": manager_id,
            "created_at": datetime(2026, 6, 1, 12, tzinfo=UTC),
        }
    ]
    day_rows = [
        {"chat_id": chat_id, "day": datetime(2026, 8, 12, tzinfo=UTC).date(), "messages": 7}
    ]
    payload = _chat_days_payload(registry, day_rows, ZoneInfo("Europe/Kyiv"))
    entry = payload["chats"][0]
    assert set(entry) == {"i", "m", "c", "d"}
    assert entry["i"] == str(chat_id)
    assert entry["d"] == {"2026-08-12": 7}


# ---------------------------------------------------------------------------
# shell discovery
# ---------------------------------------------------------------------------


def test_render_returns_none_without_a_shell(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("src.metrics.shell._SHELL_PATHS", (Path("/nope/none.html"),))
    assert load_shell() is None
    assert render_with_shell({"a": 1}) is None


def test_image_path_wins_over_local_dev_build(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    image = tmp_path / "image.html"
    dev = tmp_path / "dev.html"
    image.write_text("IMAGE", encoding="utf-8")
    dev.write_text("DEV", encoding="utf-8")
    monkeypatch.setattr("src.metrics.shell._SHELL_PATHS", (image, dev))
    assert shell_path() == image
    assert load_shell() == "IMAGE"
