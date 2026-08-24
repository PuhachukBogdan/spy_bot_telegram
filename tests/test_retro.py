"""Tests for the one-off archive retro analysis and its report.

No real DB, LLM, or network. The pieces under test are pure: the acceptance gate,
the windowing, the cost estimate, and the HTML renderer.

The gate is the point of this whole pass. The live system's reviewed history was 14
human-confirmed findings against 24 rejected, and nearly every rejection was a
topical mention with no concealment intent — so the retro contract requires a
verbatim quote plus a named marker, and this module pins that requirement.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any
from uuid import UUID, uuid4

import pytest
from pydantic import ValidationError

from src.importer.retro import (
    AVG_CHARS_PER_MESSAGE,
    MEASURED_INPUT_TOKENS_PER_MESSAGE,
    WINDOW_OVERLAP,
    WINDOW_SIZE,
    RetroStats,
    _accept,
    count_windows,
    estimate_cost,
    load_prompt,
    plan_windows,
)
from src.importer.retro_report import RunSummary, _band, render_report
from src.importer.retro_schema import (
    MIN_CONFIDENCE,
    ArchiveAnalysis,
    ArchiveFinding,
    build_archive_findings_tool,
)
from tests.conftest import Make

# ---------------------------------------------------------------------------
# Acceptance gate
# ---------------------------------------------------------------------------


def _finding(
    message_id: str,
    *,
    confidence: float = 0.9,
    quote: str = "чтоб не спалили",
    marker: str = "intent to avoid detection",
) -> ArchiveFinding:
    return ArchiveFinding(
        message_id=message_id,
        risk_type="shadow_deal",  # type: ignore[arg-type]
        score=90,
        confidence=confidence,
        quote=quote,
        marker=marker,
        explanation="Internal employee proposes hiding the arrangement.",
    )


def test_anchored_confident_finding_is_accepted() -> None:
    stats = RetroStats(run_id=uuid4())
    mid = str(uuid4())

    assert _accept(_finding(mid), {mid}, stats) is True
    assert stats.dropped_low_confidence == 0
    assert stats.dropped_unanchored == 0


def test_low_confidence_is_dropped_even_though_the_prompt_forbids_it() -> None:
    """Asking for confidence >= 0.7 is not the same as getting it."""
    stats = RetroStats(run_id=uuid4())
    mid = str(uuid4())

    assert _accept(_finding(mid, confidence=MIN_CONFIDENCE - 0.01), {mid}, stats) is False
    assert stats.dropped_low_confidence == 1


def test_finding_without_a_quote_is_dropped() -> None:
    """A finding that cannot point at its own evidence is what this pass excludes."""
    stats = RetroStats(run_id=uuid4())
    mid = str(uuid4())

    assert _accept(_finding(mid, quote="   "), {mid}, stats) is False
    assert stats.dropped_unanchored == 1


def test_empty_quote_or_marker_is_rejected_by_the_schema_itself() -> None:
    """First line of defence: the contract won't even parse an unanchored finding."""
    for field in ("quote", "marker"):
        with pytest.raises(ValidationError):
            _finding(str(uuid4()), **{field: ""})  # type: ignore[arg-type]


def test_whitespace_only_marker_is_dropped_by_the_gate() -> None:
    """Second line: `min_length` passes " " — the gate is what catches it."""
    stats = RetroStats(run_id=uuid4())
    mid = str(uuid4())

    assert _accept(_finding(mid, marker="   "), {mid}, stats) is False
    assert stats.dropped_unanchored == 1


def test_finding_anchored_on_lead_in_context_is_dropped() -> None:
    """Overlap messages are context only — a finding may not anchor on them.

    Otherwise the same episode would be reported once per window that saw it.
    """
    stats = RetroStats(run_id=uuid4())
    anchorable = {str(uuid4())}

    assert _accept(_finding(str(uuid4())), anchorable, stats) is False
    assert stats.dropped_unanchored == 1


# ---------------------------------------------------------------------------
# Windowing
# ---------------------------------------------------------------------------


def _messages(count: int) -> list[Any]:
    mk = Make()
    return [mk.message_row() for _ in range(count)]


def test_single_short_window_has_no_lead_in() -> None:
    messages = _messages(10)
    (index, window, anchorable) = plan_windows(messages)[0]

    assert index == 0
    assert len(window) == 10
    assert len(anchorable) == 10


def test_later_windows_carry_lead_in_context_that_is_not_anchorable() -> None:
    messages = _messages(WINDOW_SIZE * 2)
    windows = plan_windows(messages)

    assert len(windows) == 2
    _, second_window, second_anchorable = windows[1]
    # The window is read with overlap…
    assert len(second_window) == WINDOW_SIZE + WINDOW_OVERLAP
    # …but only its own messages can carry a finding.
    assert len(second_anchorable) == WINDOW_SIZE
    lead_in = second_window[:WINDOW_OVERLAP]
    assert all(str(m.id) not in second_anchorable for m in lead_in)


def test_every_message_is_anchorable_in_exactly_one_window() -> None:
    messages = _messages(WINDOW_SIZE * 3 + 7)
    windows = plan_windows(messages)

    seen: list[str] = []
    for _, _, anchorable in windows:
        seen.extend(anchorable)
    assert len(seen) == len(messages)
    assert len(set(seen)) == len(messages)


def test_no_windows_for_an_empty_chat() -> None:
    assert plan_windows([]) == []


# ---------------------------------------------------------------------------
# Cost estimate
# ---------------------------------------------------------------------------


def test_estimate_is_calibrated_on_a_real_trial_not_on_character_counts() -> None:
    """A bottom-up estimate from characters lands ~1.6x low.

    The per-message XML envelope — `<message id="<uuid>" sender_role=... >` — costs
    more than a typical 67-character message body, so text volume is the minority of
    the bill. The completed run measured 113.3 input tokens per message against the
    ~34 the text alone accounts for; calibrating on the measurement is what makes the
    projected spend trustworthy.
    """
    assert MEASURED_INPUT_TOKENS_PER_MESSAGE == 113.3  # 6_381_659 / 56_329

    measured = estimate_cost([56_329], input_per_mtok=3.0, output_per_mtok=15.0)
    text_only = estimate_cost(
        [56_329], input_per_mtok=3.0, output_per_mtok=15.0,
        tokens_per_message=AVG_CHARS_PER_MESSAGE / 2,
    )
    assert measured["tokens_in"] > text_only["tokens_in"] * 2.5
    # The real run cost $19.81 for this corpus; the projection must land near it.
    assert 18.0 < measured["cost_usd"] < 22.0


def test_estimate_scales_with_volume_and_reports_windows() -> None:
    small = estimate_cost([1_000], input_per_mtok=1.0, output_per_mtok=5.0)
    large = estimate_cost([50_000], input_per_mtok=1.0, output_per_mtok=5.0)

    assert large["cost_usd"] > small["cost_usd"]
    assert small["windows"] == 1_000 // WINDOW_SIZE + 1
    assert large["tokens_in"] > large["tokens_out"]


def test_estimate_never_reports_zero_windows() -> None:
    assert estimate_cost([], input_per_mtok=1.0, output_per_mtok=5.0)["windows"] == 1.0


# ---------------------------------------------------------------------------
# Prompt + schema contract
# ---------------------------------------------------------------------------


def test_prompt_is_versioned_and_encodes_the_calibration() -> None:
    text, version = load_prompt()

    assert version == "retro-1"
    # The gate.
    assert "concealment" in text.lower()
    # The confirmed marker that anchors the whole calibration…
    assert "чтоб не спалили" in text
    # …and the rejected contrast that is the sharpest signal in the corpus.
    assert "Хочешь в отдельный чат" in text
    # Each rejected class the humans actually rejected.
    for rejected in ("последний день", "перекупили", "пропушить потоки"):
        assert rejected in text


def test_tool_schema_requires_quote_and_marker() -> None:
    tool = build_archive_findings_tool()
    schema = tool["function"]["parameters"]
    finding = schema["$defs"]["ArchiveFinding"]

    assert set(finding["required"]) >= {"message_id", "risk_type", "score", "quote", "marker"}
    assert finding["properties"]["quote"]["minLength"] == 1
    assert finding["properties"]["marker"]["minLength"] == 1


def test_empty_findings_list_is_a_valid_response() -> None:
    """Most windows contain no risk; silence must not be an error."""
    assert ArchiveAnalysis().findings == []
    assert ArchiveAnalysis.model_validate({"findings": []}).findings == []


# ---------------------------------------------------------------------------
# Report
# ---------------------------------------------------------------------------


def test_severity_bands() -> None:
    assert _band(95) == "critical"
    assert _band(80) == "critical"
    assert _band(79) == "high"
    assert _band(60) == "high"
    assert _band(59) == "medium"
    assert _band(29) == "low"
    assert _band(0) == "low"


def _row(**overrides: Any) -> dict[str, Any]:
    row: dict[str, Any] = {
        "risk_type": "hidden_payment",
        "score": 90,
        "confidence": 0.85,
        "quote": "если мы попробуем обойти платёжку?",
        "marker": "proposal to bypass the payment system",
        "explanation": "Internal employee proposes bypassing the payment system.",
        "chat_name": "79732 | Q3 | Betonwin",
        "aff_id": "79732",
        "sender_name": "Uncle Bogdan",
        "sender_role": "internal",
        "occurred_at": datetime(2026, 3, 14, 9, 30, tzinfo=UTC),
        "telegram_message_id": 4212,
        "message_text": "…",
    }
    row.update(overrides)
    return row


def _summary() -> RunSummary:
    return RunSummary(
        run_id=UUID("11111111-1111-1111-1111-111111111111"),
        model="anthropic/claude-sonnet-4-6",
        prompt_version="retro-1",
        windows=476,
        messages_analysed=57_063,
        chats=240,
        tokens_in=2_817_000,
        tokens_out=167_000,
        cost_usd=10.95,
    )


def test_report_renders_a_finding_with_its_quote_and_marker() -> None:
    html = render_report(_summary(), [_row()])

    assert "обойти платёжку" in html
    assert "proposal to bypass the payment system" in html
    assert "hidden_payment" in html
    assert "critical" in html
    assert "$10.95" in html


def test_report_escapes_message_content() -> None:
    """Quotes come from partner chats — untrusted text in a file people open."""
    html = render_report(_summary(), [_row(quote="<script>alert(1)</script>")])

    assert "<script>alert(1)</script>" not in html
    assert "&lt;script&gt;" in html


def test_report_states_plainly_when_nothing_met_the_bar() -> None:
    html = render_report(_summary(), [])

    assert "No findings met the bar" in html
    # Silence is framed as a result, not a failure.
    assert "not an error" in html


def test_report_works_without_a_run_summary() -> None:
    html = render_report(None, [_row()])

    assert "no completed windows recorded" in html


def test_report_is_theme_aware_and_self_contained() -> None:
    html = render_report(_summary(), [_row()])

    assert "prefers-color-scheme" in html
    assert 'data-theme="dark"' in html or "data-theme=dark" in html
    # No external requests: the archive report must open from disk or Slack.
    assert "http://" not in html
    assert "https://" not in html


def test_report_says_findings_are_kept_out_of_the_live_tables() -> None:
    html = render_report(_summary(), [_row()])

    assert "archive_retro_findings" in html
    assert "risk_events" in html


# ---------------------------------------------------------------------------
# The permanent link
# ---------------------------------------------------------------------------


def test_permanent_link_is_disabled_unless_both_secrets_are_set(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Fail-closed: a never-rotating, never-expiring link to risk findings is the
    highest-exposure artefact here, so publishing it must be a deliberate act.

    Unlike the weekly dashboard — whose token rotates on every generation and whose
    predecessor is revoked — nothing ever invalidates this URL. If it leaks, it has
    leaked permanently, which is why the route refuses to answer without a password
    rather than defaulting to open.
    """
    from pydantic import SecretStr

    from src.config import settings
    from src.main import _archive_credentials

    monkeypatch.setattr(settings, "ARCHIVE_REPORT_TOKEN", None)
    monkeypatch.setattr(settings, "ARCHIVE_REPORT_PASSWORD", None)
    assert _archive_credentials() is None

    # Token alone is not enough.
    monkeypatch.setattr(settings, "ARCHIVE_REPORT_TOKEN", SecretStr("tok"))
    assert _archive_credentials() is None

    # Password alone is not enough.
    monkeypatch.setattr(settings, "ARCHIVE_REPORT_TOKEN", None)
    monkeypatch.setattr(settings, "ARCHIVE_REPORT_PASSWORD", SecretStr("pw"))
    assert _archive_credentials() is None

    # An empty string counts as unset — a blank env var must not open the route.
    monkeypatch.setattr(settings, "ARCHIVE_REPORT_TOKEN", SecretStr(""))
    assert _archive_credentials() is None

    monkeypatch.setattr(settings, "ARCHIVE_REPORT_TOKEN", SecretStr("tok"))
    assert _archive_credentials() == ("tok", "pw")


def test_permanent_link_route_is_registered_and_separate_from_reports() -> None:
    """It must not share a path prefix with /r or /dashboard, whose tokens rotate."""
    from src.main import app

    paths = {getattr(route, "path", "") for route in app.routes}
    assert "/archive/{token}" in paths
    # The rotating surfaces are untouched.
    assert "/r/{share_token}" in paths
    assert "/dashboard/{share_token}" in paths


def test_report_is_a_complete_html_document() -> None:
    """It is served over HTTP now, and a fragment lands the browser in quirks mode,
    where `box-sizing: border-box` is ignored and the layout collapses."""
    html = render_report(_summary(), [_row()])

    assert html.startswith("<!DOCTYPE html>")
    assert '<html lang="en">' in html
    assert "<head>" in html and "</head>" in html
    assert html.rstrip().endswith("</body></html>")
    assert '<meta name="viewport"' in html


def test_window_count_respects_chat_boundaries() -> None:
    """Windows never span chats, so a long tail of small chats costs extra.

    56 329 messages over 238 chats packs to 470 windows in theory; the real run took
    589, because a 5-message chat still consumes a whole window — and each partial
    window re-sends the entire system prompt. `total / WINDOW_SIZE` undercounts the
    bill by a quarter.
    """
    assert count_windows([WINDOW_SIZE * 3]) == 3
    assert count_windows([WINDOW_SIZE * 3 + 1]) == 4
    # Ten tiny chats are ten windows, not one.
    assert count_windows([5] * 10) == 10
    # Empty chats are not sent at all.
    assert count_windows([0, 0, 5]) == 1

    packed = count_windows([56_329])
    per_chat = count_windows([237] * 237 + [56_329 - 237 * 237])
    assert per_chat > packed
