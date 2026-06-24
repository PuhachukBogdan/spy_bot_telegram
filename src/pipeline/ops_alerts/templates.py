"""HTML message templates for ops alerts.

The bot's global parse_mode is HTML (see bot/instance.py), so we emit HTML and
escape every dynamic field with ``html.escape`` to avoid Telegram parse errors
on stray ``<`` / ``>`` / ``&`` in feed content.

These messages broadcast into partner groups (the sanctioned proactive-write
path), so they must never reveal the monitoring source: the feed's own
status-page URL and incident id are NOT rendered, and the free-text ``details``
is stripped of HTML — which also drops any source links held in tag attributes.
"""

from __future__ import annotations

import re
from html import escape, unescape

from src.pipeline.ops_alerts.feed_parser import Incident
from src.pipeline.ops_alerts.holidays_calendar import Holiday

_TAG_RE = re.compile(r"<[^>]+>")
_WS_RE = re.compile(r"\s+")


def _esc(value: str | None) -> str:
    return escape(value) if value else "—"


def _clean(text: str | None) -> str:
    """Feed free-text → safe plain text for a partner-facing broadcast.

    Unescapes entities, strips every HTML tag (also dropping any source URLs that
    live inside tag attributes, e.g. ``<a href="https://status.…">``), collapses
    whitespace, then re-escapes for Telegram's HTML parse mode.
    """
    if not text:
        return "—"
    plain = _WS_RE.sub(" ", _TAG_RE.sub(" ", unescape(text))).strip()
    return escape(plain) if plain else "—"


def format_new_incident(inc: Incident, *, detected_at: str) -> str:
    return (
        "🚨 <b>PAYMENT ALERT — NEW ISSUE</b>\n"
        f"<b>Country:</b> {_esc(inc.country)}\n"
        f"<b>Provider:</b> {_esc(inc.provider)}\n"
        f"<b>Issue:</b> {_esc(inc.issue)}\n"
        f"<b>Status:</b> {_esc(inc.status)}\n"
        f"<b>Detection time:</b> {_esc(detected_at)}\n\n"
        f"📋 <b>Details:</b> {_clean(inc.details)}"
    )


def format_update(inc: Incident, *, updated_at: str) -> str:
    header = (
        "✅ <b>RESOLVED</b>" if inc.is_resolved else "🔄 <b>UPDATE</b>"
    )
    return (
        f"{header}\n\n"
        f"<b>Country:</b> {_esc(inc.country)}\n"
        f"<b>Provider:</b> {_esc(inc.provider)}\n"
        f"<b>Issue:</b> {_esc(inc.issue)}\n"
        f"<b>Status:</b> {_esc(inc.status)}\n"
        f"<b>Last update:</b> {_esc(updated_at)}\n\n"
        f"📋 <b>Details:</b>\n{_clean(inc.details)}"
    )


def format_holiday(holiday: Holiday) -> str:
    return (
        "🎊 <b>ARGENTINA HOLIDAY REMINDER</b>\n"
        f"{_esc(holiday.name)}\n"
        f"<b>Date:</b> {holiday.date.isoformat()}\n"
        f"<b>Population celebrating:</b> {holiday.population}%\n"
        f"<b>Business impact:</b> {_esc(holiday.impact)}\n\n"
        "⚠️ <b>Expect potential changes in:</b>\n"
        "- Deposits activity\n"
        "- Registration → FTD conversion\n"
        "- Customer support volume\n\n"
        "📊 <b>Plan accordingly for tomorrow's operations</b>"
    )
