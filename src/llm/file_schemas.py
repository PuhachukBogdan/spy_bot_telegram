"""Structured-output contract for document file-content risk analysis.

Separate from the conversation schema (RiskAnalysis / RiskFinding) because
file analysis targets a single document rather than a conversation window:
there is no per-message attribution, no context_message_ids, and findings
are always stored under the data_leak risk_type.
"""

from __future__ import annotations

from enum import StrEnum
from typing import Any

from pydantic import BaseModel, ConfigDict, Field

FILE_RISK_TOOL_NAME = "report_file_risks"


class FileCategory(StrEnum):
    """Confidential-data categories the LLM detects in a shared document."""

    CREDENTIALS = "credentials"
    FINANCIAL = "financial"
    PERSONAL_DATA = "personal_data"
    BUSINESS_SECRETS = "business_secrets"
    INTERNAL_INFRA = "internal_infra"
    LEGAL = "legal"


class FileRiskFinding(BaseModel):
    """One confidential-data finding in a document."""

    model_config = ConfigDict(extra="forbid")

    category: FileCategory = Field(description="Confidential-data category")
    excerpt: str = Field(
        description="Verbatim excerpt from the document that triggered this finding (max 200 chars)"
    )
    explanation: str = Field(
        description="One sentence explaining why this constitutes a data-leak risk"
    )
    score: int = Field(ge=0, le=100, description="Risk score 0-100")
    confidence: float = Field(ge=0.0, le=1.0, description="Confidence 0-1")


class FileRiskAnalysis(BaseModel):
    """Complete tool payload for document file-content risk analysis."""

    model_config = ConfigDict(extra="forbid")

    findings: list[FileRiskFinding] = Field(default_factory=list)


def build_file_risk_tool() -> dict[str, Any]:
    """Return the OpenAI/OpenRouter function-tool dict for forced file-risk analysis."""
    return {
        "type": "function",
        "function": {
            "name": FILE_RISK_TOOL_NAME,
            "description": (
                "Report every confidential-data finding in the document. "
                "Return an empty findings list if the document is innocuous. "
                "Each finding must include a verbatim excerpt from the document."
            ),
            "parameters": FileRiskAnalysis.model_json_schema(),
        },
    }
