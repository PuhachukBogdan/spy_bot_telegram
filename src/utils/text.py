"""Small text helpers shared across surfaces (Slack cards, HTML reports)."""

from __future__ import annotations

import re

# The LLM explanation is written for the full /risk record and can run several
# sentences. On the alert card and in the report we want a glanceable gist:
# at most the first 1-2 sentences, hard-capped so one run-on sentence can't
# blow out the layout.
_MAX_SENTENCES = 2
_MAX_CHARS = 220
_SENTENCE_SPLIT = re.compile(r"(?<=[.!?])\s+")


def short_why(text: str | None) -> str:
    """Reduce an LLM explanation to 1-2 short sentences for compact display.

    Keeps the leading sentences (where the LLM states the core reason), then
    truncates with an ellipsis if it is still too long. Returns "" for empty
    input so callers can gate the field out entirely.
    """
    s = (text or "").strip()
    if not s:
        return ""
    short = " ".join(_SENTENCE_SPLIT.split(s)[:_MAX_SENTENCES]).strip()
    if len(short) > _MAX_CHARS:
        short = short[:_MAX_CHARS].rstrip() + "…"
    return short
