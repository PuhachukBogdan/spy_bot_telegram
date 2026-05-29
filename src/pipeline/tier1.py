"""Tier-1 rule-based matcher. Phase 6.

Runs immediately on every ingested message (CLAUDE.md 7.1 step 6): cheap,
in-memory, no LLM. Matches the message text against the ``red_flag_patterns``
dictionary (literal substring + regex, case-insensitive), then scores it with a
few context modifiers.

The dictionary is held in a process-wide ``PatternCache``: loaded once at
startup and hot-reloaded every few minutes (the reload loop lives in
``pipeline.workers``). Matching against the whole enabled set regardless of the
pattern's ``language`` is deliberate: per-message language detection is noisy and
a Russian needle simply won't match English text anyway.

Scoring (heuristic; final calibration belongs to analytics, ТЗ Table 11/12):
  base       = strongest single matched pattern's base_score
  + financial  if the text mentions money/amounts
  + internal   if the sender is one of our own staff
  + repetition if two or more distinct patterns fire
  capped at 100.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import datetime
from typing import Any

import asyncpg

from src.db.models import RedFlagPattern
from src.db.queries.patterns import load_enabled_patterns, patterns_fingerprint
from src.utils.logging import get_logger

log = get_logger(__name__)

# Context-modifier weights (added to the base score). Documented heuristics;
# promote to settings/DB if analytics wants to tune them without a deploy.
_FINANCIAL_MODIFIER = 20
_INTERNAL_MODIFIER = 10
_REPETITION_MODIFIER = 10
_MAX_SCORE = 100

# Rough money / amount detector for the "financial context" modifier.
_MONEY_RE = re.compile(
    r"(\$|€|£|\busd\b|\beur\b|\bруб|\bгрн|\bр\.|\b\d+\s?(к|k|тыс|млн|m)\b)",
    re.IGNORECASE,
)


@dataclass(frozen=True)
class _CompiledPattern:
    """A dictionary row prepared for fast matching."""

    pattern_id: str
    risk_category: str
    base_score: int
    pattern_type: str
    needle_lower: str | None  # set for literal patterns
    regex: re.Pattern[str] | None  # set for regex patterns


@dataclass(frozen=True)
class MatchResult:
    """Outcome of Tier-1 matching for one message."""

    has_triggers: bool
    base_score: int
    triggered_patterns: dict[str, Any] | None


_NO_MATCH = MatchResult(has_triggers=False, base_score=0, triggered_patterns=None)


def _compile(pattern: RedFlagPattern) -> _CompiledPattern | None:
    """Prepare one dictionary row; drop (log) a row whose regex won't compile."""
    if pattern.pattern_type == "regex":
        try:
            regex = re.compile(pattern.pattern, re.IGNORECASE)
        except re.error as exc:
            log.warning(
                "tier1.bad_regex", pattern_id=str(pattern.id), error=str(exc)
            )
            return None
        return _CompiledPattern(
            pattern_id=str(pattern.id),
            risk_category=pattern.risk_category,
            base_score=pattern.base_score,
            pattern_type="regex",
            needle_lower=None,
            regex=regex,
        )
    return _CompiledPattern(
        pattern_id=str(pattern.id),
        risk_category=pattern.risk_category,
        base_score=pattern.base_score,
        pattern_type="literal",
        needle_lower=pattern.pattern.lower(),
        regex=None,
    )


class PatternCache:
    """Process-wide, hot-reloadable snapshot of the enabled Tier-1 dictionary."""

    def __init__(self) -> None:
        self._compiled: list[_CompiledPattern] = []
        self._fingerprint: tuple[int, datetime | None] | None = None
        self._loaded = False

    @property
    def loaded(self) -> bool:
        return self._loaded

    @property
    def size(self) -> int:
        return len(self._compiled)

    async def refresh(self, conn: asyncpg.Connection) -> bool:
        """Reload patterns if the dictionary changed; return ``True`` if reloaded."""
        fingerprint = await patterns_fingerprint(conn)
        if self._loaded and fingerprint == self._fingerprint:
            return False
        rows = await load_enabled_patterns(conn)
        self._compiled = [cp for cp in map(_compile, rows) if cp is not None]
        self._fingerprint = fingerprint
        self._loaded = True
        log.info("tier1.patterns_loaded", count=len(self._compiled))
        return True

    def match(self, text: str | None, sender_role: str) -> MatchResult:
        """Match ``text`` against the cached dictionary and score the result."""
        if not text:
            return _NO_MATCH
        text_lower = text.lower()

        matches: list[dict[str, Any]] = []
        for cp in self._compiled:
            hit = (
                cp.needle_lower is not None and cp.needle_lower in text_lower
            ) or (cp.regex is not None and cp.regex.search(text) is not None)
            if hit:
                matches.append(
                    {
                        "pattern_id": cp.pattern_id,
                        "risk_category": cp.risk_category,
                        "base_score": cp.base_score,
                        "pattern_type": cp.pattern_type,
                    }
                )

        if not matches:
            return _NO_MATCH

        base = max(m["base_score"] for m in matches)
        modifiers: dict[str, int] = {}
        if _MONEY_RE.search(text) is not None:
            modifiers["financial"] = _FINANCIAL_MODIFIER
        if sender_role == "internal":
            modifiers["internal"] = _INTERNAL_MODIFIER
        if len({m["pattern_id"] for m in matches}) >= 2:
            modifiers["repetition"] = _REPETITION_MODIFIER

        score = min(_MAX_SCORE, base + sum(modifiers.values()))
        return MatchResult(
            has_triggers=True,
            base_score=score,
            triggered_patterns={
                "matches": matches,
                "modifiers": modifiers,
                "base": base,
                "score": score,
            },
        )


# Module-wide singleton used by ingest / edit handlers and the reload loop.
pattern_cache = PatternCache()
