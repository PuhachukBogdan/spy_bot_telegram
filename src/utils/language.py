"""langdetect wrapper. Phase 5.

Best-effort language detection for ingested message text, stored in
``messages.detected_language`` (used later by Tier-1 per-language patterns and by
summaries). Detection is deterministic (seeded) and never raises: too-short or
feature-less text returns ``None``.
"""

from __future__ import annotations

from langdetect import DetectorFactory, LangDetectException, detect

# Seed the detector so the same text always yields the same code (langdetect is
# non-deterministic by default).
DetectorFactory.seed = 0

# Below this many non-whitespace characters, detection is noise; skip it.
_MIN_CHARS = 8


def detect_language(text: str | None) -> str | None:
    """Return an ISO-639-1 code (e.g. ``'ru'``, ``'en'``) for ``text``, or ``None``.

    Returns ``None`` for empty / too-short text and whenever langdetect cannot
    find language features (e.g. only digits, emoji, or links).
    """
    if not text or len(text.strip()) < _MIN_CHARS:
        return None
    try:
        return str(detect(text))  # langdetect is untyped; pin the return to str
    except LangDetectException:
        return None
