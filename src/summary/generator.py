"""Orchestrator for generating and delivering HTML summary reports. Phase 16.

Invoked by the in-process scheduler (``workers.summary_scheduler_loop``) on the
weekly/monthly cadence, or on demand via POST /summary/generate.
Flow:
  1. Query DB: managers, heatmap, events for the period.
  2. Build HTML via builder.build_report_html().
  3. Save rendered HTML to summaries table (with access_password).
  4. Create a dashboard row (records the release; the link itself is fixed).
  5. Post the dashboard link to Slack — viewers sign in as themselves.
  6. Stamp delivery_status=delivered.
"""

from __future__ import annotations

import secrets
from collections.abc import Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from typing import Any, Literal

from aiogram import Bot

from src.alerts.slack import (
    SlackDeliveryError,
    get_slack_client,
    send_dm_to_user,
)
from src.bot.notify import notify_internal_user, notify_telegram_id
from src.config import settings
from src.db.client import acquire_connection
from src.db.models import InternalUser
from src.db.queries.activity_signals import count_proposals, list_proposal_dates
from src.db.queries.etc import (
    list_report_recipients,
    list_slack_report_recipients,
)
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
from src.utils.session import login_url

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
    full success — surfaced in the /summary/generate JSON response. It describes
    the CHANNEL post only, and is ``True`` when ``REPORT_POST_TO_CHANNEL`` is off
    (nothing was attempted, so nothing failed); the personal copies report
    themselves in the log, never here — one unreachable reader is not a failed
    release. ``dashboard_password`` is the access password for the dashboard URL.
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
    bot: Bot | None = None,
) -> ReportResult:
    """Build, persist, and announce one summary report.

    ``until`` is the exclusive end of the reporting window. The scheduler passes
    the SCHEDULED slot instant (local midnight in ``REPORT_TIMEZONE``) so
    consecutive windows are exactly contiguous — a tick that runs a few minutes
    late must not leave a gap that no report covers. On-demand callers omit it
    and get a window ending now.

    ``bot``, when given, also DMs everyone who may read the report that a new
    one is out. Optional so the on-demand HTTP route and scripts/trigger_summary
    keep working without one; the scheduler always passes it.

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
    if settings.REPORT_POST_TO_CHANNEL:
        try:
            new_ts = await _post_slack_link(
                period_type, since, until, len(event_rows), dashboard_url
            )
        except Exception as exc:
            slack_delivered = False
            slack_error = str(exc)
            log.warning(
                "summary.post_link_failed",
                error=slack_error,
                channel=settings.SLACK_CHANNEL_REPORTS,
            )
    else:
        log.info("summary.channel_post_disabled", period_type=period_type)

    if settings.REPORT_POST_TO_CHANNEL:
        # Only retire the old link once the NEW one is confirmed posted — otherwise
        # a Slack outage would leave the channel with no working link at all.
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
    else:
        # No post to wait on, so nothing gates the rotation: the new row becomes
        # the active dashboard immediately. Safe because the advertised URL is
        # fixed and secret-free — retiring a row no longer invalidates a link
        # anyone holds, it only records which release is current.
        async with acquire_connection() as conn:
            revoked = await revoke_dashboards_except(conn, dash_id)
        log.info("summary.old_links_revoked", count=revoked, channel_post=False)

    async with acquire_connection() as conn:
        await mark_summary_delivered(conn, summary_id)

    # Independent of ``bot``: the Slack copy is what the CEO actually reads, and
    # it must go out whether or not this caller has a Telegram bot to hand.
    # Wrapped because a Slack outage must not fail a report that is already
    # built, stored and (usually) posted.
    try:
        await _announce_to_slack_dms(
            period_type, since, until, len(event_rows), dashboard_url
        )
    except Exception as exc:
        log.warning("summary.slack_dm_announce_failed", error=str(exc))

    if bot is not None:
        await _announce_to_readers(bot, period_type, since, until, len(event_rows))

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


def _seeded_telegram_readers(
    recipients: Sequence[InternalUser],
) -> list[tuple[int, str]]:
    """``REPORT_TELEGRAM_DM_IDS`` minus everyone the roles table already reaches.

    The seed exists for readers the bot knows only by Telegram id — the CEO has
    no ``internal_users`` row at all, so no role query can address him. The
    moment such a person registers they appear in BOTH lists, and the overlap is
    dropped here so they get one message rather than two.
    """
    covered = {
        account for user in recipients for account in user.telegram_accounts
    }
    return [
        (int(chat_id), role)
        for chat_id, role in settings.REPORT_TELEGRAM_DM_IDS.items()
        if int(chat_id) not in covered
    ]


async def _announce_to_readers(
    bot: Bot,
    period_type: str,
    since: datetime,
    until: datetime,
    event_count: int,
) -> None:
    """DM every Telegram reader that a new report is out, and pin it. Never raises.

    Why a DM and not just the Slack post: the Slack channel announces to whoever
    is in the channel, while the address list here IS the permission — it is read
    from ``internal_users`` at send time, so granting ``head`` adds a reader and
    revoking it removes one, with no second list to keep in step.

    ``REPORT_TELEGRAM_DM_IDS`` is the exception that proves that rule: someone who
    has never been onboarded holds no role, and waiting for their ``/register``
    would mean withholding a report they asked to be sent. Those ids carry their
    own role for the scoping rule below and are dropped from the seed as soon as
    the same account is reachable through a role holder.

    **The message is pinned, replacing last week's (2026-09-22).** The report is a
    place you go back to during the week, not a notification you read once, so it
    lives at the top of the chat until the next release pushes it out. That is
    also why a role holder's link is their own sign-in URL rather than the bare
    ``/dashboard``: a pinned message whose link only works while a 90-day cookie
    happens to be alive is a pinned dead end. The link outlives the week it
    covers (``DASHBOARD_LOGIN_LINK_DAYS``) and the next release replaces it.

    A seeded id gets the plain ``/dashboard`` instead — there is no row to sign a
    token for, and one cannot be minted for a person the bot cannot identify.

    Delivery is best-effort per person (both senders swallow a blocked or
    never-started chat): a report release must not fail because one reader never
    opened the bot.
    """
    async with acquire_connection() as conn:
        recipients = await list_report_recipients(conn)
    seeded = _seeded_telegram_readers(recipients)
    if not recipients and not seeded:
        log.info("summary.dm.no_recipients", period_type=period_type)
        return

    label = "Weekly" if period_type == "weekly" else "Monthly"
    noun = "signal" if event_count == 1 else "signals"
    period = f"{since.strftime('%d %b')} \u2013 {until.strftime('%d %b %Y')}"
    # Same rule as the Slack copy: the signal total is company-wide, and a head's
    # page does not contain their own rows, so only an admin gets it.
    counted = f"{period} \u00b7 {event_count} risk {noun}"

    def message(role: str, url: str) -> str:
        body = counted if role == "admin" else period
        return (
            f"\U0001F4CA <b>{label} team report</b>\n\n{body}\n\n"
            f'<a href="{url}">Open the report</a>'
        )

    plain_url = f"{settings.SERVER_BASE_URL.rstrip('/')}/dashboard"
    for user in recipients:
        await notify_internal_user(
            bot, user, message(user.role, login_url(user.id)), pin=True
        )
    for chat_id, role in seeded:
        await notify_telegram_id(bot, chat_id, message(role, plain_url), pin=True)
    log.info(
        "summary.dm.sent",
        period_type=period_type,
        recipients=len(recipients),
        seeded=len(seeded),
    )


def _dashboard_url(dash_token: str) -> str:
    """The link posted to Slack — fixed, and carrying no secret.

    Since 2026-09-11 the dashboard identifies its reader instead of trusting a
    token plus a shared password, so the URL is the same every week and what a
    given person sees depends on who they signed in as. ``dash_token`` is still
    minted and rotated (it records that a report was released, and old links
    redirect here), it is simply no longer part of the address.
    """
    base = settings.SERVER_BASE_URL.rstrip("/")
    return f"{base}/dashboard"


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


def _report_message(
    period_type: str,
    since: datetime,
    until: datetime,
    event_count: int,
    dashboard_url: str,
    *,
    with_counts: bool = True,
) -> tuple[str, list[dict[str, Any]]]:
    """The release announcement: notification fallback text + Block Kit card.

    One card, two destinations — the #reports channel and the Slack DM of every
    reader who asked to be reached there. Built once so the private copy can
    never drift from the public one.

    ``with_counts=False`` drops the company-wide signal total. A head's page
    excludes their own rows, so a total they cannot reconcile against it would
    leak precisely what the scoping removes (§21); they get the announcement
    and the link, and the numbers on the page are theirs.
    """
    label = "Weekly" if period_type == "weekly" else "Monthly"
    since_str = since.strftime("%d %b %Y")
    until_str = until.strftime("%d %b %Y")
    noun = "event" if event_count == 1 else "events"

    fallback = f"{label} Risk Report ({since_str} – {until_str}) is now available."
    if with_counts:
        fallback += f" {event_count} risk {noun} recorded."
    headline = (
        f":bar_chart:  *{label} Partner Risk Report*\n"
        f"*Period:* {since_str} – {until_str}"
    )
    if with_counts:
        headline += f"\n*Risk events recorded:* {event_count}"
    blocks: list[dict[str, Any]] = [
        {"type": "divider"},
        {
            "type": "section",
            "text": {"type": "mrkdwn", "text": headline},
        },
        {
            "type": "section",
            "fields": [
                {
                    "type": "mrkdwn",
                    "text": (
                        ":key: *Sign in with Telegram*\nor send `/dashboard` "
                        "to the bot for a link."
                    ),
                },
                {
                    "type": "mrkdwn",
                    "text": (
                        ":lock: *Personal access* — the page shows what your "
                        "own account is allowed to see."
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
    return fallback, blocks


async def _post_slack_link(
    period_type: str,
    since: datetime,
    until: datetime,
    event_count: int,
    dashboard_url: str,
) -> str:
    """Post the report card to the reports channel; return its ``ts``."""
    fallback, blocks = _report_message(
        period_type, since, until, event_count, dashboard_url
    )
    channel = settings.SLACK_CHANNEL_REPORTS
    client = get_slack_client()
    resp = await client.chat_postMessage(channel=channel, text=fallback, blocks=blocks)
    log.info("summary.slack_posted", channel=channel, period_type=period_type)
    ts = resp.get("ts")
    return str(ts) if ts else ""


async def _announce_to_slack_dms(
    period_type: str,
    since: datetime,
    until: datetime,
    event_count: int,
    dashboard_url: str,
) -> None:
    """DM the report card to everyone who reads the report in Slack.

    Separate from the channel post because a channel announces to whoever
    happens to be in it, and separate from the Telegram DM because the person
    who asked for this does not read the bot — Slack is where they already
    confirm and dismiss alert cards.

    The address list is the union of two sources: ``REPORT_SLACK_DM_IDS`` from
    .env (people who have no ``internal_users`` row yet, so no role to key a
    lookup on) and every enabled admin/head with a linked Slack account. An id
    appearing in both is DM'd once.

    Delivery is best-effort per person: one unreachable Slack account must not
    stop the rest, exactly like the Telegram side.
    """
    async with acquire_connection() as conn:
        rows = await list_slack_report_recipients(conn)

    # (slack id, role). The role decides whether the card may carry the
    # company-wide signal total: only an admin reads an unscoped page. A seeded
    # id has no row to read a role from, so it is taken from the grants map —
    # the role that account will hold — and anything unknown counts as scoped.
    # Fail-closed: the worst case is a card with one line less.
    targets: list[tuple[str, str]] = []
    seen: set[str] = set()
    seeded = [
        (uid, settings.REGISTRATION_ROLE_GRANTS.get(uid.strip().upper(), "head"))
        for uid in settings.REPORT_SLACK_DM_IDS
    ]
    from_db = [(r.slack_user_id or "", r.role) for r in rows]
    for raw, role in [*seeded, *from_db]:
        uid = raw.strip().upper()
        if uid and uid not in seen:
            seen.add(uid)
            targets.append((uid, role))

    if not targets:
        log.info("summary.slack_dm.no_recipients", period_type=period_type)
        return

    variants = {
        with_counts: _report_message(
            period_type,
            since,
            until,
            event_count,
            dashboard_url,
            with_counts=with_counts,
        )
        for with_counts in (True, False)
    }
    delivered = 0
    for uid, role in targets:
        fallback, blocks = variants[role == "admin"]
        try:
            await send_dm_to_user(uid, fallback, blocks=blocks)
            delivered += 1
        except SlackDeliveryError as exc:
            log.warning("summary.slack_dm.failed", slack_user_id=uid, error=str(exc))
    log.info(
        "summary.slack_dm.sent",
        period_type=period_type,
        delivered=delivered,
        addressed=len(targets),
    )
