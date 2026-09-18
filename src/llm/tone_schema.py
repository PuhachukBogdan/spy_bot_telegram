"""Structured-output contract for the daily tone-of-voice pass (Phase 2, track F).

Separate from :mod:`src.llm.schemas` on purpose: that module is the RISK contract,
whose findings become ``risk_events`` and can page someone in Slack. Nothing here
can. A tone flag is an observation about how an employee wrote to a partner, kept
in its own tables and shown only on the Team summary.

The contract is a **flag list, not a grade sheet**. The model is never asked to
rate every message — it reports only the clear cases, one row per (message,
metric), each carrying a verbatim quote and a confidence. Silence is the expected
answer for most days. That shape is what lets the pass stay lenient by
construction: a metric's rate is ``flagged / assessed``, and a message the model
did not mention counts as fine.
"""

from __future__ import annotations

from enum import StrEnum
from typing import Any

from pydantic import BaseModel, ConfigDict, Field

#: Forced-tool name; the runner sets ``tool_choice`` to this.
TONE_TOOL_NAME = "report_tone_flags"


class ToneMetric(StrEnum):
    """The five dimensions reviewed. Keys are stable — they are stored in the DB."""

    #: Rudeness, contempt, aggression toward the partner. Flag = bad.
    TOXICITY = "toxicity"
    #: The reply skipped part of what the partner actually asked. Flag = gap.
    COMPLETENESS = "completeness"
    #: Register clearly off for the relationship (curt where the partner is
    #: formal, over-familiar with a new partner, no acknowledgement at all). Flag = gap.
    COURTESY = "courtesy"
    #: A visibly unhappy partner met with dismissal or a counter-complaint instead
    #: of acknowledgement plus a concrete step. Flag = gap.
    DEESCALATION = "deescalation"
    #: The manager moved first: a heads-up, a suggestion, an offer of help the
    #: partner had not asked for. Flag = GOOD (the only positive-event metric).
    INITIATIVE = "initiative"


class ToneFlag(BaseModel):
    """One clear case on one message."""

    model_config = ConfigDict(extra="forbid")

    message_id: str = Field(description="UUID of the manager message this is about")
    metric: ToneMetric = Field(description="Which dimension the case belongs to")
    confidence: float = Field(
        ge=0.0,
        le=1.0,
        description=(
            "How sure you are this is a clear case and not a matter of taste. "
            "Below 0.7 means: do not report it."
        ),
    )
    quote: str = Field(
        min_length=1,
        description=(
            "The words that make this a case, quoted VERBATIM from the message "
            "(a fragment is fine). If you cannot quote them, do not report."
        ),
    )
    reason: str = Field(
        min_length=1,
        max_length=300,
        description="One sentence: what the partner said or asked, and what the reply did",
    )


class ToneAssessment(BaseModel):
    """Tool payload for one conversation window."""

    model_config = ConfigDict(extra="forbid")

    flags: list[ToneFlag] = Field(
        default_factory=list,
        description=(
            "Clear cases only. An empty list is the normal result for a day of "
            "ordinary professional chat."
        ),
    )


def build_tone_tool() -> dict[str, Any]:
    """OpenRouter/OpenAI function-tool definition for the forced call.

    Parameters are derived from :class:`ToneAssessment` so the schema can never
    drift from the model the arguments parse into (same rule as the risk tool).
    """
    return {
        "type": "function",
        "function": {
            "name": TONE_TOOL_NAME,
            "description": (
                "Report clear tone-of-voice cases in the manager messages marked "
                "assess=\"true\". Every flag must quote the message verbatim. "
                "Return an empty list when nothing stands out — that is expected."
            ),
            "parameters": ToneAssessment.model_json_schema(),
        },
    }
