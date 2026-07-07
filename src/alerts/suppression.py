"""Alert suppression matching — pure, deterministic (idea #2/#5/#6).

A confirmed false positive can be turned into a narrow rule (via the Slack
'🔕 Suppress' button) so the SAME signal never alerts again — without touching
the LLM or the risk category. Matching is intentionally narrow: a rule fires only
when its pattern appears in the event's own ``detected_phrase`` (optionally scoped
to a risk_type), so it can never silence a whole category and hide a new risk.
"""

from __future__ import annotations

import re

from src.db.models import RiskEvent, SuppressionRule

_WS_RE = re.compile(r"\s+")


def normalize(text: str | None) -> str:
    """Lowercase + collapse whitespace, for stable phrase comparison."""
    return _WS_RE.sub(" ", (text or "").strip().lower())


def is_suppressed(event: RiskEvent, rules: list[SuppressionRule]) -> bool:
    """True if an active rule matches this event (so its alert should be skipped).

    A rule matches when: its ``risk_type`` is unset or equals the event's, AND its
    normalized ``pattern`` is a substring of the event's normalized
    ``detected_phrase``. An event with no detected_phrase can never be matched
    (nothing to compare) — it always alerts.
    """
    phrase = normalize(event.detected_phrase)
    if not phrase:
        return False
    for rule in rules:
        if rule.risk_type is not None and rule.risk_type != event.risk_type:
            continue
        pattern = normalize(rule.pattern)
        if pattern and pattern in phrase:
            return True
    return False
