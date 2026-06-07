"""Unit tests for the risk scoring logic (src/pipeline/scoring.py).

Pure functions — no DB / network. Asserts against the config bands locked
2026-06-07 (medium >=30, high >=60, critical >=80; alert at high+).
"""

from __future__ import annotations

import pytest

from src.config import settings
from src.pipeline.scoring import (
    MULTIPLIER_CONFIRMED,
    MULTIPLIER_LIKELY_FP,
    MULTIPLIER_UNCERTAIN,
    VERDICT_CONFIRMED,
    VERDICT_LIKELY_FP,
    VERDICT_UNCERTAIN,
    score_finding,
    score_to_risk_level,
    should_alert,
    verdict_from_confidence,
)

# --- verdict_from_confidence -------------------------------------------------


def test_high_confidence_confirms_and_amplifies() -> None:
    assert verdict_from_confidence(0.9) == (VERDICT_CONFIRMED, MULTIPLIER_CONFIRMED)
    # boundary: exactly 0.7 confirms
    assert verdict_from_confidence(0.7) == (VERDICT_CONFIRMED, MULTIPLIER_CONFIRMED)


def test_low_confidence_is_likely_fp_and_dampens() -> None:
    assert verdict_from_confidence(0.1) == (VERDICT_LIKELY_FP, MULTIPLIER_LIKELY_FP)


def test_mid_confidence_is_uncertain_and_neutral() -> None:
    assert verdict_from_confidence(0.5) == (VERDICT_UNCERTAIN, MULTIPLIER_UNCERTAIN)
    # boundary: exactly 0.3 is NOT a false positive (>= cutoff) -> uncertain
    assert verdict_from_confidence(0.3) == (VERDICT_UNCERTAIN, MULTIPLIER_UNCERTAIN)


# --- score_to_risk_level (config bands) --------------------------------------


@pytest.mark.parametrize(
    ("score", "level"),
    [
        (0, "low"),
        (29, "low"),
        (30, "medium"),
        (59, "medium"),
        (60, "high"),
        (79, "high"),
        (80, "critical"),
        (100, "critical"),
    ],
)
def test_score_to_risk_level_bands(score: int, level: str) -> None:
    assert score_to_risk_level(score) == level


def test_bands_match_config() -> None:
    assert score_to_risk_level(settings.RISK_LEVEL_MEDIUM_MIN) == "medium"
    assert score_to_risk_level(settings.RISK_LEVEL_HIGH_MIN) == "high"
    assert score_to_risk_level(settings.RISK_LEVEL_CRITICAL_MIN) == "critical"


# --- should_alert (config-gated at high+) ------------------------------------


def test_only_high_and_critical_alert() -> None:
    assert should_alert("critical") is True
    assert should_alert("high") is True
    assert should_alert("medium") is False
    assert should_alert("low") is False


# --- score_finding -----------------------------------------------------------


def test_confirmed_llm_only_finding_scores_off_llm_alone() -> None:
    # The dictionary never fired (rule 0) but the LLM is confident: must still
    # score — this is the whole point of the LLM being the independent judge.
    result = score_finding(rule_base_score=0, llm_score=75, llm_confidence=0.85)
    assert result.llm_verdict == VERDICT_CONFIRMED
    assert result.final_score == 90  # max(0,75) * 1.2 = 90
    assert result.risk_level == "critical"
    assert result.disagreement is False
    assert should_alert(result.risk_level) is True


def test_likely_fp_dampens() -> None:
    result = score_finding(rule_base_score=70, llm_score=70, llm_confidence=0.1)
    assert result.llm_verdict == VERDICT_LIKELY_FP
    assert result.final_score == 28  # 70 * 0.4
    assert result.risk_level == "low"


def test_rule_score_never_inflates_final() -> None:
    # The dictionary screamed (70) but the LLM rated severity low (40). Severity is
    # the LLM's alone: final = 40 * 1.2 = 48, NOT max(70,40)*1.2. The rule is only
    # a routing hint and must not raise the score.
    result = score_finding(rule_base_score=70, llm_score=40, llm_confidence=0.85)
    assert result.final_score == 48
    assert result.risk_level == "medium"


def test_disagreement_is_informational_not_capping() -> None:
    # Rule screamed (80) but the LLM says benign (severity 10): the low LLM score
    # already keeps the final low/unalerted — the flag is informational only.
    result = score_finding(rule_base_score=80, llm_score=10, llm_confidence=0.5)
    assert result.disagreement is True
    assert result.final_score == 10  # 10 * 1.0, purely from the LLM
    assert result.risk_level == "low"
    assert should_alert(result.risk_level) is False


def test_no_disagreement_when_rule_below_priority_threshold() -> None:
    result = score_finding(rule_base_score=40, llm_score=10, llm_confidence=0.5)
    assert result.disagreement is False


def test_final_score_clamped_to_100() -> None:
    result = score_finding(rule_base_score=100, llm_score=100, llm_confidence=0.95)
    assert result.final_score == 100  # 100 * 1.2 clamped


def test_base_score_preserved_in_result() -> None:
    result = score_finding(rule_base_score=55, llm_score=40, llm_confidence=0.5)
    assert result.base_score == 55
    assert result.llm_score == 40
