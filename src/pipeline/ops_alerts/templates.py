"""HTML message templates for ops alerts.

The bot's global parse_mode is HTML (see bot/instance.py), so we emit HTML and
escape every dynamic field with ``html.escape`` to avoid Telegram parse errors
on stray ``<`` / ``>`` / ``&`` in feed content.

These messages broadcast into partner groups (the sanctioned proactive-write
path), so they must never reveal the monitoring source or the payment provider:
only the country and the incident's last-update time are ever rendered. The
feed's free-text fields (issue / status / details) and its links / incident id
are deliberately NOT shown.
"""

from __future__ import annotations

from html import escape

from src.pipeline.ops_alerts.feed_parser import Incident
from src.pipeline.ops_alerts.holidays_calendar import Holiday


def _esc(value: str | None) -> str:
    return escape(value) if value else "—"


def format_new_incident(inc: Incident, *, last_update: str) -> str:
    return (
        "<b>PSP alert</b>\n"
        f"<b>Country:</b> {_esc(inc.country)}\n"
        f"<b>Last update:</b> {_esc(last_update)}\n\n"
        "⚠️ <b>Expect potential changes in:</b>\n"
        "-  click2reg\n"
        "-  reg2dep"
    )


def format_recovery(inc: Incident, *, last_update: str) -> str:
    return (
        "✅ <b>PSP recovered</b>\n"
        f"<b>Country:</b> {_esc(inc.country)}\n"
        f"<b>Last update:</b> {_esc(last_update)}"
    )


def format_holiday(holiday: Holiday) -> str:
    return (
        "🎊 <b>ARGENTINA HOLIDAY REMINDER</b>\n"
        f"{_esc(holiday.name)}\n"
        f"<b>Date:</b> {holiday.date.isoformat()}\n\n"
        "⚠️ <b>Expect potential changes in:</b>\n"
        "-  click2reg\n"
        "-  reg2dep"
    )
