"""Unit tests for the Tier-2 structured-output contract (src/llm/schemas.py)."""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from src.llm.schemas import (
    RISK_ANALYSIS_TOOL_NAME,
    RiskAnalysis,
    RiskFinding,
    RiskLevel,
    RiskType,
    build_risk_analysis_tool,
)


def test_risk_categories_count() -> None:
    assert len(RiskType) == 13  # 12 conversation categories + data_leak (file analysis)
    assert RiskType.PRIVATE_CHANNEL == "private_channel"  # value matches DB string


def test_four_risk_levels() -> None:
    assert [lvl.value for lvl in RiskLevel] == ["low", "medium", "high", "critical"]


def test_finding_parses_valid_payload() -> None:
    f = RiskFinding(
        message_id="m1",
        risk_type="private_channel",  # type: ignore[arg-type]
        score=75,
        confidence=0.92,
        explanation="moved to a private DM",
        context_message_ids=["m1", "m2"],
    )
    assert f.risk_type is RiskType.PRIVATE_CHANNEL
    assert f.context_message_ids == ["m1", "m2"]


def test_finding_rejects_out_of_range_score() -> None:
    with pytest.raises(ValidationError):
        RiskFinding(
            message_id="m1",
            risk_type=RiskType.SHADOW_DEAL,
            score=150,  # > 100
            confidence=0.5,
            explanation="x",
        )


def test_finding_rejects_bad_confidence_and_unknown_category() -> None:
    with pytest.raises(ValidationError):
        RiskFinding(
            message_id="m1",
            risk_type=RiskType.SHADOW_DEAL,
            score=10,
            confidence=1.5,  # > 1.0
            explanation="x",
        )
    with pytest.raises(ValidationError):
        RiskFinding(
            message_id="m1",
            risk_type="not_a_real_category",  # type: ignore[arg-type]
            score=10,
            confidence=0.5,
            explanation="x",
        )


def test_finding_rejects_extra_fields() -> None:
    with pytest.raises(ValidationError):
        RiskFinding.model_validate(
            {
                "message_id": "m1",
                "risk_type": "shadow_deal",
                "score": 10,
                "confidence": 0.5,
                "explanation": "x",
                "injected": "ignore me",  # extra=forbid
            }
        )


def test_analysis_empty_is_valid() -> None:
    assert RiskAnalysis().risk_events == []


def test_analysis_parses_tool_arguments() -> None:
    payload = {
        "risk_events": [
            {
                "message_id": "m1",
                "risk_type": "hidden_payment",
                "score": 80,
                "confidence": 0.9,
                "explanation": "off-book payment proposed",
                "context_message_ids": ["m1"],
            }
        ]
    }
    analysis = RiskAnalysis.model_validate(payload)
    assert analysis.risk_events[0].risk_type is RiskType.HIDDEN_PAYMENT


def test_tool_schema_shape() -> None:
    tool = build_risk_analysis_tool()
    assert tool["type"] == "function"
    assert tool["function"]["name"] == RISK_ANALYSIS_TOOL_NAME
    params = tool["function"]["parameters"]
    assert "risk_events" in params["properties"]
    # parameters must be the schema RiskAnalysis itself validates against
    assert params == RiskAnalysis.model_json_schema()
