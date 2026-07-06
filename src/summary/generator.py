"""Orchestrator for generating and delivering HTML summary reports. Phase 16.

Invoked by the in-process scheduler (``workers.summary_scheduler_loop``) on the
weekly/monthly cadence, or on demand via POST /summary/generate.
Flow:
  1. Query DB: managers, heatmap, events for the period.
  2. Build HTML via builder.build_report_html().
  3. Save rendered HTML to summaries table (with access_password).
  4. Create a dashboard row (share_token + password).
  5. Post dashboard URL + password to Slack.
  6. Stamp delivery_status=delivered.
"""

from __future__ import annotations

import secrets
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from typing import Any, Literal

from src.alerts.slack import get_slack_client
from src.config import settings
from src.db.client import acquire_connection
from src.db.queries.activity_signals import count_proposals, list_proposal_dates
from src.db.queries.summaries import (
    create_dashboard,
    list_active_managers,
    list_events_for_report,
    mark_summary_delivered,
    risk_heatmap,
    save_summary,
)
from src.summary.builder import build_report_html
from src.utils.logging import get_logger

log = get_logger(__name__)

_PASSWD_CHARS = "ABCDEFGHJKMNPQRSTUVWXYZ23456789"


def _gen_password() -> str:
    """8-char password from an unambiguous uppercase+digit alphabet."""
    return "".join(secrets.choice(_PASSWD_CHARS) for _ in range(8))


@dataclass
class ReportResult:
    """Outcome of one report generation, returned to the API caller.

    ``slack_delivered`` distinguishes "report saved but Slack post failed"
    (the silent-failure case that hid the missing-channel bug in pilot) from
    full success — surfaced in the /summary/generate JSON response.
    ``dashboard_password`` is the access password for the dashboard URL.
    """

    url: str
    event_count: int
    slack_delivered: bool
    slack_error: str | None = None
    dashboard_password: str | None = field(default=None)


async def generate_report(
    *, period_type: Literal["weekly", "monthly"]
) -> ReportResult:
    """Build, persist, and announce one summary report.

    Returns a :class:`ReportResult` describing the dashboard URL, access
    password, and whether the Slack announcement actually went out.
    """
    span = timedelta(days=7 if period_type == "weekly" else 30)
    until = datetime.now(UTC)
    since = until - span
    expires_at = until + span

    async with acquire_connection() as conn:
        managers = await list_active_managers(conn)
        heatmap_rows = await risk_heatmap(conn, since, until)
        event_rows = await list_events_for_report(conn, since, until)
        # Monthly needs per-proposal dates for the client-side range filter;
        # weekly only needs the total count.
        if period_type == "monthly":
            proposal_dates = await list_proposal_dates(conn, since, until)
            proposals = len(proposal_dates)
        else:
            proposals = await count_proposals(conn, since, until)
            proposal_dates = None

    html = build_report_html(
        period_type=period_type,
        since=since,
        until=until,
        managers=managers,
        heatmap_rows=heatmap_rows,
        event_rows=event_rows,
        proposals_count=proposals,
        proposal_dates=proposal_dates,
    )

    report_pw = _gen_password()
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
            access_password=report_pw,
        )

    dash_pw = _gen_password()
    dash_token = secrets.token_hex(32)
    dash_expires = until + timedelta(days=30)
    async with acquire_connection() as conn:
        await create_dashboard(
            conn,
            share_token=dash_token,
            access_password=dash_pw,
            expires_at=dash_expires,
        )

    dashboard_url = _dashboard_url(dash_token)
    slack_delivered = True
    slack_error: str | None = None
    try:
        await _post_slack_link(
            period_type, since, until, len(event_rows), dashboard_url, dash_pw
        )
    except Exception as exc:
        slack_delivered = False
        slack_error = str(exc)
        log.warning(
            "summary.post_link_failed",
            error=slack_error,
            channel=settings.SLACK_CHANNEL_REPORTS,
        )

    async with acquire_connection() as conn:
        await mark_summary_delivered(conn, summary_id)

    log.info(
        "summary.generated",
        period_type=period_type,
        since=since.isoformat(),
        until=until.isoformat(),
        event_count=len(event_rows),
        dashboard_url=dashboard_url,
        slack_delivered=slack_delivered,
    )
    return ReportResult(
        url=dashboard_url,
        event_count=len(event_rows),
        slack_delivered=slack_delivered,
        slack_error=slack_error,
        dashboard_password=dash_pw,
    )


def _dashboard_url(dash_token: str) -> str:
    base = settings.SERVER_BASE_URL.rstrip("/")
    return f"{base}/dashboard/{dash_token}"


async def _post_slack_link(
    period_type: str,
    since: datetime,
    until: datetime,
    event_count: int,
    dashboard_url: str,
    password: str,
) -> None:
    label = "Weekly" if period_type == "weekly" else "Monthly"
    channel = settings.SLACK_CHANNEL_REPORTS
    since_str = since.strftime("%d %b %Y")
    until_str = until.strftime("%d %b %Y")
    noun = "event" if event_count == 1 else "events"

    fallback = (
        f"{label} Risk Report ({since_str} – {until_str}) is now available. "
        f"{event_count} risk {noun} recorded. Password: {password}"
    )
    blocks: list[dict[str, Any]] = [
        {"type": "divider"},
        {
            "type": "section",
            "text": {
                "type": "mrkdwn",
                "text": (
                    f":bar_chart:  *{label} Partner Risk Report*\n"
                    f"*Period:* {since_str} – {until_str}\n"
                    f"*Risk events recorded:* {event_count}"
                ),
            },
        },
        {
            "type": "section",
            "fields": [
                {
                    "type": "mrkdwn",
                    "text": f":key: *Access password:*\n`{password}`",
                },
                {
                    "type": "mrkdwn",
                    "text": (
                        ":lock: *Access restricted* — do not forward "
                        "this message outside the team."
                    ),
                },
            ],
        },
        {
            "type": "actions",
            "elements": [
                {
                    "type": "button",
                    "text": {"type": "plain_text", "text": "Open Dashboard"},
                    "url": dashboard_url,
                    "action_id": "open_dashboard",
                    "style": "primary",
                }
            ],
        },
    ]
    client = get_slack_client()
    await client.chat_postMessage(channel=channel, text=fallback, blocks=blocks)
    log.info("summary.slack_posted", channel=channel, period_type=period_type)
