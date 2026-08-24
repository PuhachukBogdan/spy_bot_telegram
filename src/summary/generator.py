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
    count_chats_added,
    count_proposals_by_chat,
    create_dashboard,
    get_active_dashboard,
    list_active_chats,
    list_chat_added_dates,
    list_events_by_chat,
    mark_summary_delivered,
    revoke_dashboards_except,
    save_summary,
    set_dashboard_slack,
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


async def _collect_and_build(
    period_type: Literal["weekly", "monthly"],
    since: datetime,
    until: datetime,
) -> tuple[str, list[dict[str, Any]]]:
    """Query the window and render the report HTML. Returns (html, event_rows).

    Shared by :func:`generate_report` (full release) and :func:`refresh_report`
    (daily content refresh) so the two can never render different reports from
    the same window.
    """
    async with acquire_connection() as conn:
        chats = await list_active_chats(conn)
        event_rows = await list_events_by_chat(conn, since, until)
        chats_added = await count_chats_added(conn, since, until)
        proposals_by_chat = await count_proposals_by_chat(conn, since, until)
        # Monthly needs per-item dates for the client-side range filter; weekly
        # only needs the totals.
        if period_type == "monthly":
            proposal_dates = await list_proposal_dates(conn, since, until)
            proposals = len(proposal_dates)
            chats_added_dates = await list_chat_added_dates(conn, since, until)
        else:
            proposals = await count_proposals(conn, since, until)
            proposal_dates = None
            chats_added_dates = None

    html = build_report_html(
        period_type=period_type,
        since=since,
        until=until,
        chats=chats,
        event_rows=event_rows,
        proposals_count=proposals,
        proposal_dates=proposal_dates,
        chats_added=chats_added,
        chats_added_dates=chats_added_dates,
        proposals_by_chat=proposals_by_chat,
    )
    return html, event_rows


async def refresh_report(
    *,
    period_type: Literal["weekly", "monthly"],
    until: datetime | None = None,
) -> int:
    """Re-render the report for the current rolling window WITHOUT announcing it.

    The daily counterpart to :func:`generate_report`: it stores a fresh summary
    row and nothing else — no dashboard row, no Slack post, no link rotation, no
    ``mark_summary_delivered``. ``/dashboard/{token}`` always renders the NEWEST
    non-expired summary of each type, so the link already advertised in Slack
    starts showing this content on the next page load. That is what lets a risk
    event from yesterday evening be visible in the morning instead of waiting
    for the next Monday release.

    The row is left ``delivery_status='pending'``, which is precisely how
    ``summary_exists_since`` tells a refresh from a release: a refresh must never
    satisfy the release dedup, or the Monday Slack post would stop firing.
    Already-released rows are never mutated, so an issued ``/r/{token}`` link
    keeps serving the exact snapshot it was issued for.

    Returns the number of risk events in the refreshed window.
    """
    span = timedelta(days=7 if period_type == "weekly" else 30)
    until = until or datetime.now(UTC)
    since = until - span

    html, event_rows = await _collect_and_build(period_type, since, until)
    async with acquire_connection() as conn:
        await save_summary(
            conn,
            period_type=period_type,
            period_start=since,
            period_end=until,
            rendered_html=html,
            event_count=len(event_rows),
            share_token=secrets.token_hex(32),
            expires_at=until + span,
            access_password=_gen_password(),
        )
    log.info(
        "summary.refreshed",
        period_type=period_type,
        since=since.isoformat(),
        until=until.isoformat(),
        event_count=len(event_rows),
    )
    return len(event_rows)


async def generate_report(
    *,
    period_type: Literal["weekly", "monthly"],
    until: datetime | None = None,
) -> ReportResult:
    """Build, persist, and announce one summary report.

    ``until`` is the exclusive end of the reporting window. The scheduler passes
    the SCHEDULED slot instant (local midnight in ``REPORT_TIMEZONE``) so
    consecutive windows are exactly contiguous — a tick that runs a few minutes
    late must not leave a gap that no report covers. On-demand callers omit it
    and get a window ending now.

    Returns a :class:`ReportResult` describing the dashboard URL, access
    password, and whether the Slack announcement actually went out.
    """
    span = timedelta(days=7 if period_type == "weekly" else 30)
    until = until or datetime.now(UTC)
    since = until - span
    expires_at = until + span

    html, event_rows = await _collect_and_build(period_type, since, until)

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
        # The link currently advertised in Slack (to retire once the new one is
        # live). Read BEFORE inserting the new row.
        prev_dash = await get_active_dashboard(conn)
        dash_id = await create_dashboard(
            conn,
            share_token=dash_token,
            access_password=dash_pw,
            expires_at=dash_expires,
        )

    dashboard_url = _dashboard_url(dash_token)
    slack_delivered = True
    slack_error: str | None = None
    new_ts: str | None = None
    try:
        new_ts = await _post_slack_link(
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

    # Only retire the old link once the NEW one is confirmed posted — otherwise a
    # Slack outage would leave the channel with no working link at all.
    if slack_delivered and new_ts:
        channel = settings.SLACK_CHANNEL_REPORTS
        async with acquire_connection() as conn:
            await set_dashboard_slack(conn, dash_id, channel, new_ts)
            revoked = await revoke_dashboards_except(conn, dash_id)
        log.info("summary.old_links_revoked", count=revoked)
        if prev_dash and prev_dash.get("slack_ts"):
            # Best-effort: edit the previous message to drop its (now-dead) button.
            try:
                await _supersede_message(
                    prev_dash.get("slack_channel") or channel,
                    str(prev_dash["slack_ts"]),
                )
            except Exception as exc:
                log.warning("summary.supersede_failed", error=str(exc))

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


async def _supersede_message(channel: str, ts: str) -> None:
    """Edit a previous report message so its link is clearly retired.

    Drops the "Open Dashboard" button and marks the message superseded, so no one
    clicks a now-revoked link. Kept (not deleted) to preserve the audit trail of
    when reports were posted.
    """
    text = "This report link has been replaced by a newer report."
    blocks: list[dict[str, Any]] = [
        {"type": "divider"},
        {
            "type": "section",
            "text": {
                "type": "mrkdwn",
                "text": (
                    ":outbox_tray:  *This report has been superseded.*\n"
                    "A newer report was posted below — open the latest message "
                    "for the live dashboard. This link is no longer active."
                ),
            },
        },
    ]
    client = get_slack_client()
    await client.chat_update(channel=channel, ts=ts, text=text, blocks=blocks)
    log.info("summary.superseded_prev", channel=channel, ts=ts)


async def _post_slack_link(
    period_type: str,
    since: datetime,
    until: datetime,
    event_count: int,
    dashboard_url: str,
    password: str,
) -> str:
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
    resp = await client.chat_postMessage(channel=channel, text=fallback, blocks=blocks)
    log.info("summary.slack_posted", channel=channel, period_type=period_type)
    ts = resp.get("ts")
    return str(ts) if ts else ""
