"""Orchestrator for generating and delivering HTML summary reports. Phase 16.

Called by POST /summary/generate (triggered by n8n cron or manually).
Flow:
  1. Query DB: managers, heatmap, events for the period.
  2. Build HTML via builder.build_report_html().
  3. Save rendered HTML to summaries table.
  4. Post a Slack link to the appropriate channel.
  5. Stamp delivery_status=delivered.
"""

from __future__ import annotations

import secrets
from datetime import UTC, datetime, timedelta
from typing import Any, Literal

from src.alerts.slack import get_slack_client
from src.config import settings
from src.db.client import acquire_connection
from src.db.queries.summaries import (
    list_active_managers,
    list_events_for_report,
    mark_summary_delivered,
    risk_heatmap,
    save_summary,
)
from src.summary.builder import build_report_html
from src.utils.logging import get_logger

log = get_logger(__name__)


async def generate_report(*, period_type: Literal["weekly", "monthly"]) -> str:
    """Build, persist, and announce one summary report.

    Returns the public URL of the generated report.
    """
    until = datetime.now(UTC).replace(hour=0, minute=0, second=0, microsecond=0)
    since = until - timedelta(days=7 if period_type == "weekly" else 30)
    expires_at = until + timedelta(days=7 if period_type == "weekly" else 30)

    async with acquire_connection() as conn:
        managers = await list_active_managers(conn)
        heatmap_rows = await risk_heatmap(conn, since, until)
        event_rows = await list_events_for_report(conn, since, until)

    html = build_report_html(
        period_type=period_type,
        since=since,
        until=until,
        managers=managers,
        heatmap_rows=heatmap_rows,
        event_rows=event_rows,
    )

    share_token = secrets.token_hex(32)
    async with acquire_connection() as conn:
        summary_id = await save_summary(
            conn,
            period_type=period_type,
            period_start=since,
            period_end=until,
            rendered_html=html,
            event_count=len(event_rows),
            share_token=share_token,
            expires_at=expires_at,
        )

    report_url = _report_url(share_token)
    try:
        await _post_slack_link(period_type, since, until, len(event_rows), report_url)
    except Exception as exc:
        log.warning("summary.post_link_failed", error=str(exc))

    async with acquire_connection() as conn:
        await mark_summary_delivered(conn, summary_id)

    log.info(
        "summary.generated",
        period_type=period_type,
        since=since.isoformat(),
        until=until.isoformat(),
        event_count=len(event_rows),
        url=report_url,
    )
    return report_url


def _report_url(share_token: str) -> str:
    base = settings.SERVER_BASE_URL.rstrip("/")
    return f"{base}/r/{share_token}"


async def _post_slack_link(
    period_type: str,
    since: datetime,
    until: datetime,
    event_count: int,
    report_url: str,
) -> None:
    label = "Weekly" if period_type == "weekly" else "Monthly"
    channel = (
        settings.SLACK_CHANNEL_WEEKLY
        if period_type == "weekly"
        else settings.SLACK_CHANNEL_MONTHLY
    )
    since_str = since.strftime("%d %b %Y")
    until_str = until.strftime("%d %b %Y")

    text = f":bar_chart: *{label} Risk Report* ({since_str}–{until_str}) — {event_count} events"
    blocks: list[dict[str, Any]] = [
        {
            "type": "section",
            "text": {
                "type": "mrkdwn",
                "text": (
                    f":bar_chart: *{label} Risk Report*\n"
                    f"{since_str}–{until_str}\n"
                    f"{event_count} risk event{'s' if event_count != 1 else ''}"
                ),
            },
        },
        {
            "type": "actions",
            "elements": [
                {
                    "type": "button",
                    "text": {"type": "plain_text", "text": "Open Report"},
                    "url": report_url,
                    "action_id": "open_report",
                }
            ],
        },
    ]
    try:
        client = get_slack_client()
        await client.chat_postMessage(channel=channel, text=text, blocks=blocks)
        log.info("summary.slack_posted", channel=channel, period_type=period_type)
    except Exception as exc:
        log.warning("summary.slack_post_failed", error=str(exc), channel=channel)
