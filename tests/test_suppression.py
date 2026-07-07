"""Unit tests for alert-suppression matching (idea #2/#5/#6).

Pure function — no DB. Verifies the rule is NARROW: it fires only on the same
signal (risk_type + phrase substring) and never on a different phrase / type.
"""

from __future__ import annotations

from datetime import UTC, datetime
from uuid import uuid4

from src.alerts.suppression import is_suppressed, normalize
from src.db.models import RiskEvent, SuppressionRule


def _rule(pattern: str, risk_type: str | None = None) -> SuppressionRule:
    return SuppressionRule(
        id=uuid4(),
        risk_type=risk_type,
        pattern=pattern,
        created_at=datetime.now(UTC),
    )


def _event(phrase: str | None, risk_type: str = "data_leak") -> RiskEvent:
    return RiskEvent(
        id=uuid4(),
        risk_type=risk_type,
        risk_level="high",
        base_score=0,
        final_score=72,
        detected_phrase=phrase,
        created_at=datetime.now(UTC),
    )


def test_normalize_collapses_case_and_whitespace() -> None:
    assert normalize("  Player   Commission\nReport ") == "player commission report"


def test_matches_same_type_and_substring() -> None:
    ev = _event("player_commission_report_06-07 with volumes", "data_leak")
    assert is_suppressed(ev, [_rule("commission_report", "data_leak")])


def test_matches_when_rule_type_is_any() -> None:
    ev = _event("между нами договоримся", "private_channel")
    assert is_suppressed(ev, [_rule("между нами", risk_type=None)])


def test_no_match_on_different_risk_type() -> None:
    ev = _event("commission report", "data_leak")
    assert not is_suppressed(ev, [_rule("commission report", "private_channel")])


def test_no_match_on_different_phrase() -> None:
    ev = _event("totally unrelated wording", "data_leak")
    assert not is_suppressed(ev, [_rule("commission report", "data_leak")])


def test_no_match_when_event_has_no_phrase() -> None:
    ev = _event(None, "data_leak")
    assert not is_suppressed(ev, [_rule("anything", "data_leak")])


def test_empty_rules_never_suppress() -> None:
    assert not is_suppressed(_event("anything"), [])
