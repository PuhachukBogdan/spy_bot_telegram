"""Risk scoring: turn rule + LLM signals into a risk_events row's numbers.

Foundation for the batch processor (§7.4), the priority lane (§7.5) and the
operational_sla job. Pure and synchronous — no I/O, fully unit-testable.

Two scales meet here, but only one is the verdict:
  * the Tier-1 rule ``base_score`` (0-100) — a *routing hint* (CLAUDE.md 7.1). It
    decides whether and how urgently a message reaches the LLM, nothing more. It
    is recorded on the row for reference and disagreement detection;
  * the LLM's own ``score`` (0-100 severity) and ``confidence`` (0-1) from the
    forced ``report_risk_events`` tool (:class:`src.llm.schemas.RiskFinding`).

The LLM is the SOLE authority on severity. The rule score NEVER contributes to the
final score: the LLM sees the whole conversation and decides independently whether
there is a real problem or the dictionary merely over-reacted. So a phrase the
dictionary over-rated is brought back down by the LLM, and a phrase the dictionary
never had still scores fully off the LLM.

The LLM ``confidence`` maps to a verdict and a multiplier (the risk_events
``llm_verdict`` / ``llm_multiplier`` columns):

    confidence >= CONFIRM_CONFIDENCE  -> "confirmed"  x1.2  (amplify)
    confidence <  FP_CONFIDENCE       -> "likely_fp"  x0.4  (dampen)
    otherwise                         -> "uncertain"  x1.0

``final_score = clamp(round(llm_score * multiplier), 0, 100)`` — derived purely
from the LLM's own severity and confidence.

``disagreement`` flags a rule/LLM conflict (the rules screamed, the LLM says
benign): ``rule_base_score >= PRIORITY_SCORE_THRESHOLD and llm_score <=
DISAGREEMENT_MAX_LLM_SCORE``. It is purely informational — the low LLM score
already keeps the final score low and unalerted; the flag surfaces the mismatch
for human review and for tuning the dictionary.
"""

from __future__ import annotations

from dataclasses import dataclass

from src.config import settings

# Verdict labels stored in risk_events.llm_verdict.
VERDICT_CONFIRMED = "confirmed"
VERDICT_LIKELY_FP = "likely_fp"
VERDICT_UNCERTAIN = "uncertain"

# Confidence cut points (LLM 0-1) -> verdict. Documented heuristics; the level
# bands in config are the user-facing knobs, these finer cut points live with the
# logic they drive.
CONFIRM_CONFIDENCE = 0.7
FP_CONFIDENCE = 0.3

# Multipliers applied to the effective base score (ТЗ: 1.2 / 0.4 / 1.0).
MULTIPLIER_CONFIRMED = 1.2
MULTIPLIER_LIKELY_FP = 0.4
MULTIPLIER_UNCERTAIN = 1.0

# A rule score this high paired with an LLM severity this low is a disagreement.
DISAGREEMENT_MAX_LLM_SCORE = 20

# Numeric ordering of the four levels, for the alert-floor comparison.
_LEVEL_ORDER = {"low": 0, "medium": 1, "high": 2, "critical": 3}


@dataclass(frozen=True)
class RiskScore:
    """The computed scoring fields for one risk_events row (LLM path)."""

    base_score: int  # Tier-1 rule score (as fed in)
    llm_score: int  # LLM severity 0-100
    llm_confidence: float  # LLM confidence 0-1
    llm_verdict: str  # confirmed / likely_fp / uncertain
    llm_multiplier: float  # 1.2 / 0.4 / 1.0
    final_score: int  # 0-100, post-multiplier, post-disagreement cap
    risk_level: str  # low / medium / high / critical
    disagreement: bool  # rule/LLM conflict (capped, never alerted)


def verdict_from_confidence(confidence: float) -> tuple[str, float]:
    """Map an LLM confidence (0-1) to a ``(verdict, multiplier)`` pair."""
    if confidence >= CONFIRM_CONFIDENCE:
        return VERDICT_CONFIRMED, MULTIPLIER_CONFIRMED
    if confidence < FP_CONFIDENCE:
        return VERDICT_LIKELY_FP, MULTIPLIER_LIKELY_FP
    return VERDICT_UNCERTAIN, MULTIPLIER_UNCERTAIN


def score_to_risk_level(final_score: int) -> str:
    """Map a 0-100 final score to a risk level using the config bands."""
    if final_score >= settings.RISK_LEVEL_CRITICAL_MIN:
        return "critical"
    if final_score >= settings.RISK_LEVEL_HIGH_MIN:
        return "high"
    if final_score >= settings.RISK_LEVEL_MEDIUM_MIN:
        return "medium"
    return "low"


def should_alert(risk_level: str) -> bool:
    """True if a risk at this level fires a real-time alert (config-gated).

    Levels below ``ALERT_MIN_RISK_LEVEL`` are stored only and surface in the
    weekly/monthly summary instead of pinging Slack in the moment.
    """
    return _LEVEL_ORDER[risk_level] >= _LEVEL_ORDER[settings.ALERT_MIN_RISK_LEVEL]


def score_finding(
    *, rule_base_score: int, llm_score: int, llm_confidence: float
) -> RiskScore:
    """Combine a Tier-1 rule score and one LLM finding into the stored numbers.

    ``rule_base_score`` is the Tier-1 score for the flagged message (0 when the
    dictionary did not fire); ``llm_score`` / ``llm_confidence`` come from the
    LLM finding. See the module docstring for the model.
    """
    verdict, multiplier = verdict_from_confidence(llm_confidence)
    # Severity is the LLM's alone — the rule score is a routing hint only and is
    # deliberately NOT mixed in here, so the LLM can both raise an undetected risk
    # and damp one the dictionary over-rated.
    final_score = max(0, min(100, round(llm_score * multiplier)))

    # Informational only (no effect on the final score): the rules flagged this
    # hard but the LLM judged it benign — worth surfacing for review / tuning.
    disagreement = (
        rule_base_score >= settings.PRIORITY_SCORE_THRESHOLD
        and llm_score <= DISAGREEMENT_MAX_LLM_SCORE
    )

    return RiskScore(
        base_score=rule_base_score,
        llm_score=llm_score,
        llm_confidence=llm_confidence,
        llm_verdict=verdict,
        llm_multiplier=multiplier,
        final_score=final_score,
        risk_level=score_to_risk_level(final_score),
        disagreement=disagreement,
    )
