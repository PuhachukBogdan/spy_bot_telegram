"""HTML message templates for ops alerts.

The bot's global parse_mode is HTML (see bot/instance.py), so we emit HTML and
escape every dynamic field with ``html.escape`` to avoid Telegram parse errors
on stray ``<`` / ``>`` / ``&`` in feed content.
"""

from __future__ import annotations

from html import escape

from src.pipeline.ops_alerts.feed_parser import Incident
from src.pipeline.ops_alerts.holidays_calendar import Holiday


def _esc(value: str | None) -> str:
    return escape(value) if value else "—"


def format_new_incident(inc: Incident, *, detected_at: str) -> str:
    return (
        "🚨 <b>PAYMENT ALERT — NEW ISSUE</b>\n"
        f"<b>Country:</b> {_esc(inc.country)}\n"
        f"<b>Provider:</b> {_esc(inc.provider)}\n"
        f"<b>Issue:</b> {_esc(inc.issue)}\n"
        f"<b>Status:</b> {_esc(inc.status)}\n"
        f"<b>Detection time:</b> {_esc(detected_at)}\n\n"
        f"📋 <b>Details:</b> {_esc(inc.details)}\n"
        f"🔗 <b>Link:</b> {_esc(inc.link)}\n"
        f"<i>ID: {_esc(inc.incident_id)}</i>"
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
        f"📋 <b>Details:</b>\n{_esc(inc.details)}\n\n"
        f"🔗 <b>Link:</b> {_esc(inc.link)}\n"
        f"<i>ID: {_esc(inc.incident_id)}</i>"
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
