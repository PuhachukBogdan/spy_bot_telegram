"""Risk-alert dispatch orchestration. Phase 11.

Ties the alert pieces together: cooldown (:mod:`dedup`), critical pings
(:mod:`critical`), Slack delivery (:mod:`slack`), and the failure fallback
(:mod:`failed`). Called by the batch processor AFTER the analysis transaction has
committed — never inside it — so a Slack network call never holds a DB
connection. Each pre-send lookup and the ts write-back use their own short
connections; the Slack call sits between them, holding nothing.

For each alertable risk event: resolve the cooldown thread, post the Block Kit
card (critical also @-pings recipients and mirrors into the main channel), and on
success write the Slack ts back (for threading + Phase-12 callbacks). On a total
Slack failure, hand off to :func:`handle_failed_alert`.
"""

from __future__ import annotations

from aiogram import Bot

from src.alerts.critical import critical_mention_prefix
from src.alerts.dedup import resolve_thread_ts
from src.alerts.failed import handle_failed_alert
from src.alerts.slack import SlackDeliveryError, build_alert_blocks, post_alert
from src.config import settings
from src.db.client import acquire_connection
from src.db.models import Chat, RiskEvent
from src.db.queries.risk_events import set_slack_message_ts
from src.utils.logging import get_logger

log = get_logger(__name__)


async def dispatch_alerts(
    bot: Bot, chat: Chat, partner_name: str | None, events: list[RiskEvent]
) -> None:
    """Dispatch every alertable risk event for one chat (post-commit, sequential)."""
    for event in events:
        await _dispatch_one(bot, chat, partner_name, event)


async def _dispatch_one(
    bot: Bot, chat: Chat, partner_name: str | None, event: RiskEvent
) -> None:
    short_id = str(event.id)[:8]
    is_critical = event.risk_level == "critical"

    # One short connection for the pre-send lookups; released before the network call.
    async with acquire_connection() as conn:
        thread_ts = await resolve_thread_ts(
            conn, chat_id=chat.id, risk_type=event.risk_type, is_critical=is_critical
        )
        mention_prefix = await critical_mention_prefix(conn) if is_critical else ""

    blocks, text = build_alert_blocks(
        event, chat, partner_name, mention_prefix=mention_prefix
    )
    primary_channel = settings.SLACK_CHANNEL_ALERTS

    try:
        ts = await post_alert(
            channel=primary_channel, text=text, blocks=blocks, thread_ts=thread_ts
        )
    except SlackDeliveryError as exc:
        log.error("alert.delivery_failed", risk_event_id=short_id, error=str(exc))
        await handle_failed_alert(bot, event, primary_channel, str(exc))
        return

    async with acquire_connection() as conn:
        await set_slack_message_ts(conn, event.id, ts)
    log.info(
        "alert.sent",
        risk_event_id=short_id,
        level=event.risk_level,
        risk_type=event.risk_type,
        threaded=thread_ts is not None,
        channel=primary_channel,
    )
