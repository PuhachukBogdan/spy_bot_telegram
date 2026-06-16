"""Slack alert delivery: client, Block Kit builder, dispatch error. Phase 11.

A thin async wrapper over slack-sdk's :class:`AsyncWebClient` (already a project
dependency). The client is a lazily-built module singleton with the SDK's default
async retry handlers (transient connection errors are retried inside the SDK).
Every delivery failure — API rejection or transport — is funnelled into a single
:class:`SlackDeliveryError` so the dispatch layer has one thing to catch and fall
back from; an alert-delivery failure must never crash the analysis worker.

``build_alert_blocks`` renders one risk event into Block Kit. The review surface
lives in Telegram (``/risk <id>``), so the message points there rather than
duplicating the full record.
"""

from __future__ import annotations

from typing import Any

from slack_sdk.errors import SlackApiError
from slack_sdk.http_retry.builtin_async_handlers import async_default_handlers
from slack_sdk.web.async_client import AsyncWebClient

from src.config import settings
from src.db.models import Chat, RiskEvent
from src.utils.logging import get_logger

log = get_logger(__name__)

# Channel-facing badge per alertable level (only high/critical ever reach here).
_LEVEL_BADGE = {
    "critical": "\U0001f534 CRITICAL RISK",  # 🔴
    "high": "\U0001f7e0 HIGH RISK",  # 🟠
}
# LLM explanations can be long; keep the Slack card readable (full text is in /risk).
_EXPLANATION_MAX = 600

_client: AsyncWebClient | None = None


class SlackDeliveryError(Exception):
    """Slack rejected or could not receive the alert (after the SDK's retries)."""


def get_slack_client() -> AsyncWebClient:
    """Lazily build the shared Slack web client (with default retry handlers)."""
    global _client
    if _client is None:
        _client = AsyncWebClient(
            token=settings.SLACK_BOT_TOKEN.get_secret_value(),
            retry_handlers=async_default_handlers(),
        )
    return _client


def reset_slack_client() -> None:
    """Drop the shared client (the SDK uses a per-call session, so nothing to close)."""
    global _client
    _client = None


async def send_dm_to_user(slack_user_id: str, text: str) -> None:
    """Open a DM with a Slack user and post a message (used by /register OTP flow).

    Raises :class:`SlackDeliveryError` on any API or transport failure so the
    caller can surface a friendly error in Telegram.
    """
    client = get_slack_client()
    try:
        resp = await client.conversations_open(users=[slack_user_id])
        channel_id: str = resp["channel"]["id"]
        await client.chat_postMessage(channel=channel_id, text=text)
    except SlackApiError as exc:
        raise SlackDeliveryError(str(exc.response.get("error", exc))) from exc
    except Exception as exc:
        raise SlackDeliveryError(str(exc)) from exc


async def post_alert(
    *,
    channel: str,
    text: str,
    blocks: list[dict[str, Any]],
    thread_ts: str | None = None,
) -> str:
    """Post one alert to ``channel``; return the Slack message ts.

    ``text`` is the notification/fallback string; ``blocks`` is the rich card.
    ``thread_ts`` threads the message under a prior alert (cooldown). Any failure
    raises :class:`SlackDeliveryError`.
    """
    client = get_slack_client()
    try:
        resp = await client.chat_postMessage(
            channel=channel, text=text, blocks=blocks, thread_ts=thread_ts
        )
    except SlackApiError as exc:
        raise SlackDeliveryError(str(exc.response.get("error", exc))) from exc
    except Exception as exc:  # transport / connection error after the SDK's retries
        raise SlackDeliveryError(str(exc)) from exc
    ts = resp.get("ts")
    if not ts:
        raise SlackDeliveryError(f"no ts in Slack response (error={resp.get('error')})")
    return str(ts)


def _risk_type_label(risk_type: str) -> str:
    """``private_channel`` -> ``Private Channel`` for the human-facing card."""
    return risk_type.replace("_", " ").title()


def build_alert_blocks(
    event: RiskEvent,
    chat: Chat,
    partner_name: str | None,
    *,
    mention_prefix: str = "",
    include_actions: bool = True,
) -> tuple[list[dict[str, Any]], str]:
    """Render one risk event into (Block Kit blocks, fallback text).

    ``mention_prefix`` (e.g. ``"<@U123> "``) is prepended for critical pings.
    ``include_actions=False`` omits the review buttons — used when updating an
    already-reviewed message so the buttons cannot be clicked a second time.
    """
    badge = _LEVEL_BADGE.get(event.risk_level, event.risk_level.upper())
    partner = partner_name or "—"  # —
    chat_name = chat.chat_name or f"chat {chat.telegram_chat_id}"
    risk_label = _risk_type_label(event.risk_type)
    short_id = str(event.id)[:8]
    phrase = (event.detected_phrase or "").strip()
    explanation = (event.llm_explanation or "").strip()[:_EXPLANATION_MAX]

    text = (
        f"{mention_prefix}{badge}: {risk_label} — {partner} "
        f"({event.final_score}/100)"
    )

    header = (mention_prefix + "\n" if mention_prefix else "") + (
        f"*{badge}* · score *{event.final_score}/100*"
    )
    blocks: list[dict[str, Any]] = [
        {"type": "section", "text": {"type": "mrkdwn", "text": header}},
        {
            "type": "section",
            "fields": [
                {"type": "mrkdwn", "text": f"*Partner:*\n{partner}"},
                {"type": "mrkdwn", "text": f"*Chat:*\n{chat_name}"},
                {"type": "mrkdwn", "text": f"*Risk type:*\n{risk_label}"},
                {"type": "mrkdwn", "text": f"*Verdict:*\n{event.llm_verdict or '—'}"},
            ],
        },
    ]
    if phrase:
        blocks.append(
            {"type": "section", "text": {"type": "mrkdwn", "text": f"*Detected:*\n>{phrase}"}}
        )
    if explanation:
        blocks.append(
            {"type": "section", "text": {"type": "mrkdwn", "text": f"*Why:*\n{explanation}"}}
        )
    blocks.append(
        {
            "type": "context",
            "elements": [
                {"type": "mrkdwn", "text": f"Review in Telegram: `/risk {short_id}`"}
            ],
        }
    )
    if include_actions:
        blocks.append(
            {
                "type": "actions",
                "elements": [
                    {
                        "type": "button",
                        "text": {"type": "plain_text", "text": "✓ Confirm"},
                        "style": "primary",
                        "action_id": "mark_confirmed",
                        "value": str(event.id),
                    },
                    {
                        "type": "button",
                        "text": {"type": "plain_text", "text": "✗ False Positive"},
                        "style": "danger",
                        "action_id": "mark_fp",
                        "value": str(event.id),
                    },
                    {
                        "type": "button",
                        "text": {"type": "plain_text", "text": "⬆ Escalate"},
                        "action_id": "mark_escalated",
                        "value": str(event.id),
                    },
                ],
            }
        )
    return blocks, text
