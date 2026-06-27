"""Structured-output contract for Tier-2 LLM risk analysis. Phase 8.

The LLM is never trusted to return free-form text. Tier-2 calls force a single
tool/function call whose arguments validate against :class:`RiskAnalysis`, so the
pipeline always receives typed, bounded data (CLAUDE.md / pipeline §7.6).

This module is the single source of truth for:
  * the 12 risk categories (:class:`RiskType`) and 4 severity levels
    (:class:`RiskLevel`) — mirrors the free-text ``risk_events.risk_type`` /
    ``risk_level`` columns, which carry no DB CHECK;
  * the per-finding / per-response Pydantic models the tool arguments parse into;
  * :func:`build_risk_analysis_tool`, the OpenAI/OpenRouter function-tool dict the
    client passes with ``tool_choice`` to force JSON.

It has zero I/O and zero network — pure contract, fully unit-testable.
"""

from __future__ import annotations

from enum import StrEnum
from typing import Any

from pydantic import BaseModel, ConfigDict, Field

# The forced-tool name. The client sets tool_choice to this so the model cannot
# answer in prose.
RISK_ANALYSIS_TOOL_NAME = "report_risk_events"


class ActivitySignalType(StrEnum):
    """Manager activity signal types detected alongside risk events.

    Stored in ``activity_signals.signal_type``; used by the manager-centric
    weekly/monthly summary to count proposals and closed deals per manager.
    """

    MANAGER_PROPOSAL = "manager_proposal"
    DEAL_CLOSED = "deal_closed"


class ActivitySignal(BaseModel):
    """One manager activity signal the LLM detected in the conversation.

    Only report signals where an internal employee (manager/staff) is the
    actor — partner-side proposals are not counted.
    """

    model_config = ConfigDict(extra="forbid")

    message_id: str = Field(description="UUID of the message containing the signal")
    signal_type: ActivitySignalType = Field(
        description="'manager_proposal' or 'deal_closed'"
    )
    description: str = Field(
        description="One sentence describing what was proposed or agreed"
    )


class RiskType(StrEnum):
    """The 12 conversation risk categories + data_leak for document analysis.

    Enum values are the exact strings stored in ``risk_events.risk_type``
    (pipeline §7.6 — no DB CHECK, this is the guard).
    ``DATA_LEAK`` is assigned by the file-analysis worker, never by the LLM
    conversation tool (which uses the other 12).
    """

    SHADOW_DEAL = "shadow_deal"
    PRIVATE_CHANNEL = "private_channel"
    HIDDEN_PAYMENT = "hidden_payment"
    TRAFFIC_LEAKAGE = "traffic_leakage"
    COMMERCIAL_TERMS = "commercial_terms"
    FRAUD_SHAVE = "fraud_shave"
    ACCESS_RISK = "access_risk"
    PARTNER_CHURN = "partner_churn"
    PAYMENT_CONFLICT = "payment_conflict"
    REPUTATION_RISK = "reputation_risk"
    OPERATIONAL_SLA = "operational_sla"
    EMPLOYEE_BEHAVIOR = "employee_behavior"
    DATA_LEAK = "data_leak"


class RiskLevel(StrEnum):
    """Severity tiers stored in ``risk_events.risk_level`` and used by alert
    routing (pipeline §7.7). Derived from the final score downstream — NOT part
    of the LLM tool output, which returns a numeric score instead."""

    LOW = "low"
    MEDIUM = "medium"
    HIGH = "high"
    CRITICAL = "critical"


class RiskFinding(BaseModel):
    """One risk *episode* the LLM attributes to a single anchor message (§7.6 output).

    A finding is ONE episode, not one message. When several messages form a single
    risk episode (an opening line plus its follow-ups / the agreement that confirms
    it), report ONE finding anchored on the key message and list the rest in
    ``context_message_ids`` — do not emit a separate finding per message. This is
    what keeps the alert layer from spamming one case as N alerts.

    ``score`` 0-100 and ``confidence`` 0-1 are both bounded so a malformed model
    response fails validation rather than poisoning downstream scoring. A benign-
    in-context message is returned with ``confidence < 0.3`` (a deliberate
    false-positive signal) rather than omitted.
    """

    model_config = ConfigDict(extra="forbid")

    message_id: str = Field(
        description=(
            "UUID of the single KEY message that anchors this risk episode — the "
            "most incriminating one (usually the message that first establishes the "
            "risk). Report only ONE finding per episode; never one per message."
        )
    )
    risk_type: RiskType = Field(description="One of the 12 risk categories")
    score: int = Field(ge=0, le=100, description="Risk score 0-100")
    confidence: float = Field(ge=0.0, le=1.0, description="LLM confidence 0-1")
    explanation: str = Field(
        description="One-to-two sentence justification, no chain-of-thought"
    )
    context_message_ids: list[str] = Field(
        default_factory=list,
        description=(
            "UUIDs of the OTHER messages in this same episode — surrounding lines, "
            "follow-ups, or the agreement that confirm the risk anchored above"
        ),
    )


class RiskAnalysis(BaseModel):
    """The complete tool payload: risks + manager activity signals found in the excerpt.

    Both lists default to empty — a no-tool-call response is treated as this.
    """

    model_config = ConfigDict(extra="forbid")

    risk_events: list[RiskFinding] = Field(default_factory=list)
    activity_signals: list[ActivitySignal] = Field(default_factory=list)


def build_risk_analysis_tool() -> dict[str, Any]:
    """Return the OpenAI/OpenRouter function-tool dict for forced risk analysis.

    Parameters are derived from :class:`RiskAnalysis` so the schema can never
    drift from the model the arguments parse into. Pair with
    ``tool_choice={"type": "function", "function": {"name": RISK_ANALYSIS_TOOL_NAME}}``
    to force the call.
    """
    return {
        "type": "function",
        "function": {
            "name": RISK_ANALYSIS_TOOL_NAME,
            "description": (
                "Report every genuine risk signal found in the conversation "
                "excerpt, AND any manager activity signals (proposals, closed "
                "deals). Return empty lists if nothing is found. Include "
                "benign-in-context risk matches with confidence below 0.3."
            ),
            "parameters": RiskAnalysis.model_json_schema(),
        },
    }
