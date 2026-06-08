"""System alerts for operational events. Phase 15.

Currently: daily LLM budget exceeded → circuit breaker tripped.

Delivery:
  1. Slack post to SLACK_CHANNEL_SYSTEM (falls back to SLACK_CHANNEL_ALERTS).
  2. Telegram DM to every enabled admin.

Both are best-effort: a delivery failure is logged and swallowed so a monitoring
alert can never crash the analysis worker.
"""

from __future__ import annotations

from decimal import Decimal
from html import escape as html_escape
from typing import Any

from aiogram import Bot

from src.alerts.slack import get_slack_client
from src.bot.notify import notify_admins
from src.config import settings
from src.db.client import acquire_connection
from src.db.queries.etc import list_admin_users
from src.utils.logging import get_logger

log = get_logger(__name__)


async def send_budget_exceeded_alert(
    bot: Bot,
    spend: Decimal,
    limit: Decimal,
) -> None:
    """Announce that the daily LLM budget has been hit. Best-effort, never raises."""
    spend_label = f"${spend:.4f}"
    limit_label = f"${limit:.2f}"
    reset_sql = (
        "UPDATE cost_tracking SET circuit_breaker_triggered=false "
        "WHERE date=CURRENT_DATE;"
    )

    await _post_to_slack(spend_label, limit_label, reset_sql)
    await _dm_admins(bot, spend_label, limit_label, reset_sql)


async def _post_to_slack(
    spend_label: str, limit_label: str, reset_sql: str
) -> None:
    channel = settings.SLACK_CHANNEL_SYSTEM or settings.SLACK_CHANNEL_ALERTS
    text = (
        f":rotating_light: *Daily LLM budget exceeded* — "
        f"spent {spend_label} / limit {limit_label}. "
        "LLM workers paused until reset or tomorrow."
    )
    blocks: list[dict[str, Any]] = [
        {
            "type": "header",
            "text": {
                "type": "plain_text",
                "text": "🚨 Daily LLM Budget Exceeded",
            },
        },
        {
            "type": "section",
            "fields": [
                {"type": "mrkdwn", "text": f"*Spent:* {spend_label}"},
                {"type": "mrkdwn", "text": f"*Limit:* {limit_label}"},
            ],
        },
        {
            "type": "section",
            "text": {
                "type": "mrkdwn",
                "text": (
                    "LLM analysis and file workers are *paused* until the "
                    "breaker is reset or a new calendar day starts.\n"
                    f"To reset now:\n```{reset_sql}```"
                ),
            },
        },
    ]
    try:
        client = get_slack_client()
        await client.chat_postMessage(channel=channel, text=text, blocks=blocks)
        log.info("system_alert.slack_posted", channel=channel)
    except Exception as exc:
        log.error("system_alert.slack_failed", error=str(exc))


async def _dm_admins(
    bot: Bot, spend_label: str, limit_label: str, reset_sql: str
) -> None:
    tg_text = (
        "🚨 <b>Daily LLM budget exceeded</b>\n\n"
        f"Spent: <code>{html_escape(spend_label)}</code> "
        f"/ Limit: <code>{html_escape(limit_label)}</code>\n\n"
        "LLM workers paused. Reset with:\n"
        f"<code>{html_escape(reset_sql)}</code>"
    )
    try:
        async with acquire_connection() as conn:
            admins = await list_admin_users(conn)
        await notify_admins(bot, admins, tg_text)
        log.info("system_alert.tg_dms_sent", admins=len(admins))
    except Exception as exc:
        log.error("system_alert.tg_dms_failed", error=str(exc))
