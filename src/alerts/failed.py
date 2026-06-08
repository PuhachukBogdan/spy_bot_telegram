"""Undeliverable alerts: failed_alerts row + emergency Telegram fallback. Phase 11.

When Slack — the primary alert path — fails after the SDK's retries, we must not
lose the alert. Two independent best-effort steps run, neither raising (an
alert-delivery failure must never crash the analysis worker):

  1. write a ``failed_alerts`` row (breadcrumb for inspection / later retry);
  2. DM every reachable admin via Telegram, so a human still learns of the risk
     while Slack is down.
"""

from __future__ import annotations

from html import escape as html_escape
from typing import Any

from aiogram import Bot

from src.bot.notify import notify_admins
from src.db.client import acquire_connection
from src.db.models import RiskEvent
from src.db.queries.alerts import record_failed_alert
from src.db.queries.etc import list_admin_users
from src.utils.logging import get_logger

log = get_logger(__name__)


async def handle_failed_alert(
    bot: Bot, event: RiskEvent, channel: str, error: str
) -> None:
    """Record a failed Slack alert and fall back to a Telegram DM to admins."""
    short_id = str(event.id)[:8]
    payload: dict[str, Any] = {
        "risk_level": event.risk_level,
        "risk_type": event.risk_type,
        "final_score": event.final_score,
        "detected_phrase": event.detected_phrase,
    }

    try:
        async with acquire_connection() as conn:
            await record_failed_alert(
                conn,
                risk_event_id=event.id,
                channel=channel,
                payload=payload,
                error=error,
            )
    except Exception as exc:  # the breadcrumb failing shouldn't sink the fallback
        log.error("alert.failed_log_error", risk_event_id=short_id, error=str(exc))

    text = (
        "⚠️ <b>Slack alert delivery failed</b>\n\n"  # ⚠️
        f"<b>{html_escape(event.risk_level.upper())}</b> risk "
        f"({event.final_score}/100): {html_escape(event.risk_type)}\n"
        f"Review: <code>/risk {short_id}</code>\n\n"
        f"<i>Slack error: {html_escape(error)}</i>"
    )
    try:
        async with acquire_connection() as conn:
            admins = await list_admin_users(conn)
        await notify_admins(bot, admins, text)
        log.warning("alert.telegram_fallback", risk_event_id=short_id, admins=len(admins))
    except Exception as exc:
        log.error("alert.telegram_fallback_error", risk_event_id=short_id, error=str(exc))
