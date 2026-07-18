"""System alerts for operational events. Phase 15.

Currently:
  - daily LLM budget exceeded → circuit breaker tripped;
  - Supabase database size crossing the storage-warning threshold.

Delivery (every event):
  1. Slack post to SLACK_CHANNEL_ALERTS.
  2. Telegram DM to every enabled admin.

Both channels are best-effort: a delivery failure is logged and swallowed so a
monitoring alert can never crash the worker that fired it.
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


async def _broadcast_system_alert(
    bot: Bot,
    *,
    slack_text: str,
    slack_blocks: list[dict[str, Any]],
    tg_text: str,
) -> None:
    """Post one system alert to Slack AND DM every enabled admin.

    The two channels are isolated and best-effort: a Slack failure never blocks
    the Telegram DMs and vice-versa, and neither ever raises — a monitoring alert
    must not crash the worker that fired it.
    """
    channel = settings.SLACK_CHANNEL_ALERTS
    try:
        client = get_slack_client()
        await client.chat_postMessage(
            channel=channel, text=slack_text, blocks=slack_blocks
        )
        log.info("system_alert.slack_posted", channel=channel)
    except Exception as exc:
        log.error("system_alert.slack_failed", error=str(exc))

    try:
        async with acquire_connection() as conn:
            admins = await list_admin_users(conn)
        await notify_admins(bot, admins, tg_text)
        log.info("system_alert.tg_dms_sent", admins=len(admins))
    except Exception as exc:
        log.error("system_alert.tg_dms_failed", error=str(exc))


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

    slack_text = (
        f":rotating_light: *Daily LLM budget exceeded* — "
        f"spent {spend_label} / limit {limit_label}. "
        "LLM workers paused until reset or tomorrow."
    )
    slack_blocks: list[dict[str, Any]] = [
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
    tg_text = (
        "🚨 <b>Daily LLM budget exceeded</b>\n\n"
        f"Spent: <code>{html_escape(spend_label)}</code> "
        f"/ Limit: <code>{html_escape(limit_label)}</code>\n\n"
        "LLM workers paused. Reset with:\n"
        f"<code>{html_escape(reset_sql)}</code>"
    )
    await _broadcast_system_alert(
        bot, slack_text=slack_text, slack_blocks=slack_blocks, tg_text=tg_text
    )


def _format_size_mb(num_bytes: float) -> str:
    """Human-friendly MB label with one decimal, e.g. ``412.5 MB``."""
    return f"{num_bytes / (1024 * 1024):.1f} MB"


async def send_storage_warning_alert(
    bot: Bot,
    *,
    used_bytes: int,
    limit_bytes: int,
    pct: float,
) -> None:
    """Warn that the Supabase database is filling up. Best-effort, never raises.

    Fired by ``storage_monitor_loop`` once live usage crosses
    ``STORAGE_ALERT_THRESHOLD_PERCENT`` of the plan's size cap, so an admin can
    purge old data or upgrade the plan before writes are blocked.
    """
    used_label = _format_size_mb(used_bytes)
    limit_label = _format_size_mb(limit_bytes)
    pct_label = f"{pct:.0f}%"
    threshold_label = f"{settings.STORAGE_ALERT_THRESHOLD_PERCENT}%"

    slack_text = (
        f":floppy_disk: *Supabase storage {pct_label} full* — "
        f"{used_label} / {limit_label}. Purge old data or upgrade the plan."
    )
    slack_blocks: list[dict[str, Any]] = [
        {
            "type": "header",
            "text": {
                "type": "plain_text",
                "text": f"🗄️ Supabase storage {pct_label} full",
            },
        },
        {
            "type": "section",
            "fields": [
                {"type": "mrkdwn", "text": f"*Used:* {used_label}"},
                {"type": "mrkdwn", "text": f"*Limit:* {limit_label}"},
                {"type": "mrkdwn", "text": f"*Usage:* {pct_label}"},
                {"type": "mrkdwn", "text": f"*Alert at:* {threshold_label}"},
            ],
        },
        {
            "type": "section",
            "text": {
                "type": "mrkdwn",
                "text": (
                    "The database is approaching its plan size cap. To free "
                    "space: run the retention purge "
                    "(`python scripts/cleanup_retention.py`) or shorten the "
                    "retention window; otherwise upgrade the Supabase plan and "
                    "raise `SUPABASE_DB_SIZE_LIMIT_MB`."
                ),
            },
        },
    ]
    tg_text = (
        f"🗄️ <b>Supabase storage {html_escape(pct_label)} full</b>\n\n"
        f"Used: <code>{html_escape(used_label)}</code> "
        f"/ Limit: <code>{html_escape(limit_label)}</code>\n"
        f"Alert threshold: <code>{html_escape(threshold_label)}</code>\n\n"
        "Free space: run the retention purge "
        "(<code>python scripts/cleanup_retention.py</code>) or shorten the "
        "window, or upgrade the Supabase plan and raise "
        "<code>SUPABASE_DB_SIZE_LIMIT_MB</code>."
    )
    await _broadcast_system_alert(
        bot, slack_text=slack_text, slack_blocks=slack_blocks, tg_text=tg_text
    )
    log.warning(
        "system_alert.storage_warning",
        used_bytes=used_bytes,
        limit_bytes=limit_bytes,
        pct=round(pct, 1),
    )
