"""Risk-alert dispatch orchestration. Phase 11 + risk-case rework.

Ties the alert pieces together: case resolution (:mod:`dedup`), critical pings
(:mod:`critical`), Slack delivery (:mod:`slack`), and the failure fallback
(:mod:`failed`). Called by the batch processor AFTER the analysis transaction has
committed — never inside it — so a Slack network call never holds a DB
connection. Each DB lookup and the ts write-back use their own short connections;
the Slack call sits between them, holding nothing.

One case is one card. For each alertable risk event:

  * no open case for (chat × risk_type) in the window → post a fresh Block Kit card
    (critical also @-pings recipients), and write its ts back;
  * an open case exists → the finding is the SAME case developing: re-render the
    card from the most-severe finding in the case (with a "N signals" note) and
    EDIT it in place instead of posting a second alert. The finding joins the case
    (its ts is set to the case card's). If this finding is what pushed the case
    into critical, a short threaded message re-pings the recipients — the only new
    message an update ever produces.

On a total Slack failure, hand off to :func:`handle_failed_alert`.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

from aiogram import Bot

from src.alerts.critical import critical_mention_prefix
from src.alerts.dedup import resolve_open_case_ts
from src.alerts.failed import handle_failed_alert
from src.alerts.slack import (
    SlackDeliveryError,
    build_alert_blocks,
    post_alert,
    update_alert,
)
from src.alerts.suppression import is_suppressed
from src.config import settings
from src.db.client import acquire_connection
from src.db.models import Chat, RiskEvent
from src.db.queries.messages import get_message_timestamp, get_messages_by_ids
from src.db.queries.risk_events import list_case_events, set_slack_message_ts
from src.db.queries.suppressions import list_active_suppressions
from src.utils.logging import get_logger

log = get_logger(__name__)

# Latest-signal snippet shown in the case-update note (keep the card compact).
_CASE_NOTE_PHRASE_MAX = 120


async def dispatch_alerts(
    bot: Bot, chat: Chat, partner_name: str | None, events: list[RiskEvent]
) -> None:
    """Dispatch every alertable risk event for one chat (post-commit, sequential)."""
    for event in events:
        await _dispatch_one(bot, chat, partner_name, event)


async def _dispatch_one(
    bot: Bot, chat: Chat, partner_name: str | None, event: RiskEvent
) -> None:
    is_critical = event.risk_level == "critical"

    # One short connection for the pre-send lookups; released before the network call.
    async with acquire_connection() as conn:
        rules = await list_active_suppressions(conn)
        case_ts = await resolve_open_case_ts(
            conn, chat_id=chat.id, risk_type=event.risk_type
        )
        mention_prefix = await critical_mention_prefix(conn) if is_critical else ""

    # Staff-suppressed signal (confirmed FP): the risk_event stays in the DB for
    # audit/report, but no Slack alert is posted. Narrow match — never a category.
    if is_suppressed(event, rules):
        log.info(
            "alert.suppressed",
            risk_event_id=str(event.id)[:8],
            risk_type=event.risk_type,
        )
        return

    if case_ts is None:
        await _open_case(bot, chat, partner_name, event, mention_prefix)
    else:
        await _update_case(
            bot, chat, partner_name, event, case_ts, mention_prefix, is_critical
        )


async def _open_case(
    bot: Bot,
    chat: Chat,
    partner_name: str | None,
    event: RiskEvent,
    mention_prefix: str,
) -> None:
    """No open case for this (chat × risk_type): post a fresh top-level card."""
    short_id = str(event.id)[:8]
    async with acquire_connection() as conn:
        message_dt = (
            await get_message_timestamp(conn, event.message_id)
            if event.message_id
            else None
        )
        context = await get_messages_by_ids(conn, event.context_message_ids or [])
    blocks, text = build_alert_blocks(
        event,
        chat,
        partner_name,
        mention_prefix=mention_prefix,
        message_dt=message_dt,
        context_messages=context,
    )
    channel = settings.SLACK_CHANNEL_ALERTS
    try:
        ts = await post_alert(channel=channel, text=text, blocks=blocks)
    except SlackDeliveryError as exc:
        log.error("alert.delivery_failed", risk_event_id=short_id, error=str(exc))
        await handle_failed_alert(bot, event, channel, str(exc))
        return

    async with acquire_connection() as conn:
        await set_slack_message_ts(conn, event.id, ts)
    log.info(
        "alert.case_opened",
        risk_event_id=short_id,
        level=event.risk_level,
        risk_type=event.risk_type,
        channel=channel,
    )


async def _update_case(
    bot: Bot,
    chat: Chat,
    partner_name: str | None,
    event: RiskEvent,
    case_ts: str,
    mention_prefix: str,
    is_critical: bool,
) -> None:
    """An open case is developing: edit its card in place; re-ping on escalation."""
    short_id = str(event.id)[:8]
    channel = settings.SLACK_CHANNEL_ALERTS
    since = datetime.now(UTC) - timedelta(minutes=settings.RISK_CASE_WINDOW_MINUTES)

    # The finding is already persisted (dispatch is post-commit), so it's part of
    # the case set. Render the card from the most-severe finding so escalations show
    # and a later, milder follow-up never downgrades the badge.
    async with acquire_connection() as conn:
        case_events = await list_case_events(
            conn, chat_id=chat.id, risk_type=event.risk_type, since=since
        )
    if not case_events:  # defensive: window edge / race — treat as a fresh case
        case_events = [event]
    primary = max(case_events, key=lambda e: e.final_score)
    prior_critical = any(
        e.risk_level == "critical" for e in case_events if e.id != event.id
    )
    crossed_into_critical = is_critical and not prior_critical

    phrase = (event.detected_phrase or "").strip()[:_CASE_NOTE_PHRASE_MAX]
    case_note = f"Case: {len(case_events)} signals" + (f" · latest: {phrase}" if phrase else "")
    # The card renders the most-severe finding, so show that message's send time.
    async with acquire_connection() as conn:
        message_dt = (
            await get_message_timestamp(conn, primary.message_id)
            if primary.message_id
            else None
        )
        context = await get_messages_by_ids(conn, primary.context_message_ids or [])
    # No mention on the edited card itself — re-pinging is the threaded note's job,
    # so a routine update never re-notifies the whole channel.
    blocks, text = build_alert_blocks(
        primary,
        chat,
        partner_name,
        case_note=case_note,
        message_dt=message_dt,
        context_messages=context,
    )

    try:
        await update_alert(channel=channel, ts=case_ts, text=text, blocks=blocks)
    except SlackDeliveryError as exc:
        log.error("alert.update_failed", risk_event_id=short_id, error=str(exc))
        await handle_failed_alert(bot, event, channel, str(exc))
        return

    # The finding now belongs to the case card (keeps the case open + anchored).
    async with acquire_connection() as conn:
        await set_slack_message_ts(conn, event.id, case_ts)

    if crossed_into_critical:
        await _ping_escalation(event, case_ts, mention_prefix, channel)

    log.info(
        "alert.case_updated",
        risk_event_id=short_id,
        level=event.risk_level,
        risk_type=event.risk_type,
        signals=len(case_events),
        escalated=crossed_into_critical,
        channel=channel,
    )


async def _ping_escalation(
    event: RiskEvent, case_ts: str, mention_prefix: str, channel: str
) -> None:
    """Threaded re-ping when a finding pushes an open case into critical (best-effort)."""
    text = (
        f"{mention_prefix}\U0001f534 Escalated to CRITICAL — "
        f"score {event.final_score}/100"
    )
    blocks = [{"type": "section", "text": {"type": "mrkdwn", "text": text}}]
    try:
        await post_alert(channel=channel, text=text, blocks=blocks, thread_ts=case_ts)
    except SlackDeliveryError as exc:
        # The card was already updated; a failed re-ping must not crash dispatch.
        log.warning(
            "alert.escalation_ping_failed",
            risk_event_id=str(event.id)[:8],
            error=str(exc),
        )
