"""Structured-output contract for the one-off archive retro analysis.

Separate from :mod:`src.llm.schemas` on purpose. The live Tier-2 contract asks for
a score plus a confidence and lets scoring downstream decide severity; this one
adds two required fields the live contract has no equivalent of — a verbatim
``quote`` and a named ``marker`` — because the whole premise of the retro pass is
that a finding must be able to point at the concealment language it rests on.

A model that cannot fill those two fields cannot produce a finding, which is the
mechanism that keeps out the topical-mention false positives that dominated the
live system's rejected alerts.
"""

from __future__ import annotations

from typing import Any

from pydantic import BaseModel, ConfigDict, Field

from src.llm.schemas import RiskType

#: Forced-tool name; the client sets ``tool_choice`` to this.
ARCHIVE_FINDINGS_TOOL_NAME = "report_archive_findings"

#: Findings below this confidence are dropped client-side even if the model
#: returns them. The prompt asks for 0.7; this enforces it rather than trusting it.
MIN_CONFIDENCE = 0.7


class ArchiveFinding(BaseModel):
    """One confirmed-grade retrospective finding."""

    model_config = ConfigDict(extra="forbid")

    message_id: str = Field(
        description="UUID of the message carrying the concealment marker"
    )
    risk_type: RiskType = Field(description="One of the risk categories")
    score: int = Field(ge=0, le=100, description="Severity if the finding is real")
    confidence: float = Field(
        ge=0.0, le=1.0, description="How sure you are this is genuine, not a false positive"
    )
    quote: str = Field(
        min_length=1,
        description=(
            "The concealment/bypass language, quoted VERBATIM from the message. "
            "If you cannot quote it, do not report the finding."
        ),
    )
    marker: str = Field(
        min_length=1,
        description=(
            "Short name for what makes this a risk — e.g. 'intent to avoid "
            "detection', 'proposal to bypass the payment system', 'work without a "
            "contract', 'internal fraud data handed to partner'"
        ),
    )
    explanation: str = Field(
        min_length=1, description="One or two sentences: who did what, and why it matters"
    )
    context_message_ids: list[str] = Field(
        default_factory=list,
        description="UUIDs of other messages in the same episode",
    )


class ArchiveAnalysis(BaseModel):
    """Tool payload for one analysis window."""

    model_config = ConfigDict(extra="forbid")

    findings: list[ArchiveFinding] = Field(
        default_factory=list,
        description="Confirmed-grade findings only. An empty list is the expected "
        "result for most windows.",
    )


def build_archive_findings_tool() -> dict[str, Any]:
    """OpenRouter/OpenAI function-tool definition for the forced call."""
    return {
        "type": "function",
        "function": {
            "name": ARCHIVE_FINDINGS_TOOL_NAME,
            "description": (
                "Report retrospective risk findings for the archived conversation. "
                "Report only findings that quote an explicit concealment, bypass, or "
                "off-record marker. Return an empty list when there are none."
            ),
            "parameters": ArchiveAnalysis.model_json_schema(),
        },
    }
