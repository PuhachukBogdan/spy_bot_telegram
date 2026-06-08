"""Slack interactive button callbacks. Phase 12.

POST /slack/callback pipeline:
  1. verify_slack_signature  — HMAC-SHA256(SLACK_SIGNING_SECRET, v0:{ts}:{body}).
  2. Parse URL-encoded ``payload=<json>`` (Slack's block_actions format).
  3. Dispatch mark_fp / mark_confirmed / mark_escalated → DB + audit.
  4. Update the originating Slack message: swap action buttons for a
     reviewed-by context line (best-effort; failure is logged, never raised).
"""

from __future__ import annotations

import hashlib
import hmac
import json
import time
import urllib.parse
from typing import Any
from uuid import UUID

from starlette.datastructures import Headers

from src.alerts.slack import build_alert_blocks, get_slack_client
from src.config import settings
from src.db.client import acquire_connection
from src.db.models import RiskEvent
from src.db.queries.audit import insert_audit_log
from src.db.queries.chats import get_chat_by_id
from src.db.queries.partners import get_partner_by_id
from src.db.queries.risk_events import update_status
from src.utils.logging import get_logger

log = get_logger(__name__)

_VERSION = "v0"
_REPLAY_WINDOW = 300  # 5 min — Slack's standard replay-attack guard

_ACTION_TO_STATUS: dict[str, str] = {
    "mark_fp": "false_positive",
    "mark_confirmed": "confirmed",
    "mark_escalated": "escalated",
}

_STATUS_LABELS: dict[str, str] = {
    "false_positive": "False Positive",
    "confirmed": "Confirmed",
    "escalated": "Escalated",
}


def verify_slack_signature(headers: Headers, raw_body: bytes) -> bool:
    """Return True iff the request carries a valid Slack signature.

    Algorithm: HMAC-SHA256(signing_secret, "v0:{timestamp}:{body}"),
    then constant-time compare against the X-Slack-Signature header.
    Requests older than 5 minutes are rejected (replay-attack guard).
    """
    timestamp = headers.get("x-slack-request-timestamp", "")
    provided = headers.get("x-slack-signature", "")

    try:
        ts_int = int(timestamp)
    except ValueError:
        return False

    if abs(time.time() - ts_int) > _REPLAY_WINDOW:
        return False

    secret = settings.SLACK_SIGNING_SECRET.get_secret_value()
    base = f"{_VERSION}:{timestamp}:{raw_body.decode('utf-8')}"
    expected = _VERSION + "=" + hmac.new(
        key=secret.encode("utf-8"),
        msg=base.encode("utf-8"),
        digestmod=hashlib.sha256,
    ).hexdigest()
    return hmac.compare_digest(expected, provided)


async def handle_slack_action(raw_body: bytes) -> None:
    """Parse and dispatch one Slack block_actions callback.

    Logs and returns on malformed payloads or unknown actions so the
    endpoint always responds 200 to Slack.
    """
    try:
        payload = _parse_payload(raw_body)
    except Exception as exc:
        log.warning("slack_callback.bad_payload", error=str(exc))
        return

    if payload.get("type") != "block_actions":
        return

    actions: list[dict[str, Any]] = payload.get("actions") or []
    if not actions:
        return

    action = actions[0]
    action_id: str = action.get("action_id", "")
    status = _ACTION_TO_STATUS.get(action_id)
    if status is None:
        log.warning("slack_callback.unknown_action", action_id=action_id)
        return

    value: str = action.get("value", "")
    try:
        risk_event_id = UUID(value)
    except (ValueError, AttributeError):
        log.error("slack_callback.bad_uuid", value=value)
        return

    slack_user: dict[str, Any] = payload.get("user") or {}
    slack_user_id: str = slack_user.get("id", "")
    slack_user_name: str = (
        slack_user.get("name") or slack_user.get("username") or "unknown"
    )

    event = await _do_mark(risk_event_id, status, slack_user_id, slack_user_name)
    if event is None:
        log.warning("slack_callback.event_not_found", risk_event_id=str(risk_event_id))
        return

    msg_ts: str = (payload.get("message") or {}).get("ts") or ""
    if msg_ts:
        await _update_slack_message(event, msg_ts, status, slack_user_name)


def _parse_payload(raw_body: bytes) -> dict[str, Any]:
    """Decode Slack's ``payload=<url-encoded-json>`` form body."""
    params = dict(urllib.parse.parse_qsl(raw_body.decode("utf-8")))
    result: dict[str, Any] = json.loads(params["payload"])
    return result


async def _do_mark(
    risk_event_id: UUID,
    status: str,
    slack_user_id: str,
    slack_user_name: str,
) -> RiskEvent | None:
    """Update risk_event status and write audit row in one transaction."""
    async with acquire_connection() as conn, conn.transaction():
        event = await update_status(conn, risk_event_id, status, reviewed_by=None)
        if event is None:
            return None
        await insert_audit_log(
            conn,
            action=f"slack_{status}",
            target_entity="risk_events",
            target_id=risk_event_id,
            payload={
                "slack_user_id": slack_user_id,
                "slack_user_name": slack_user_name,
                "new_status": status,
            },
        )
    log.info(
        "slack_callback.marked",
        risk_event_id=str(risk_event_id),
        status=status,
        by=slack_user_name,
    )
    return event


async def _update_slack_message(
    event: RiskEvent,
    msg_ts: str,
    status: str,
    reviewer: str,
) -> None:
    """Replace the action buttons with a reviewed-by context line. Best-effort."""
    try:
        channel = _channel_for_level(event.risk_level)

        if event.chat_id is None:
            return
        async with acquire_connection() as conn:
            chat = await get_chat_by_id(conn, event.chat_id)
        if chat is None:
            return

        async with acquire_connection() as conn:
            partner = (
                await get_partner_by_id(conn, event.partner_id)
                if event.partner_id is not None
                else None
            )
        partner_name = partner.name if partner is not None else None

        blocks, _ = build_alert_blocks(event, chat, partner_name, include_actions=False)
        label = _STATUS_LABELS.get(status, status.replace("_", " ").title())
        blocks.append(
            {
                "type": "context",
                "elements": [
                    {"type": "mrkdwn", "text": f"✓ Marked *{label}* by @{reviewer}"}
                ],
            }
        )

        client = get_slack_client()
        await client.chat_update(channel=channel, ts=msg_ts, blocks=blocks)
        log.info("slack_callback.message_updated", ts=msg_ts, status=status)

    except Exception as exc:
        log.warning("slack_callback.update_failed", error=str(exc), ts=msg_ts)


def _channel_for_level(risk_level: str) -> str:
    if risk_level == "critical":
        return settings.SLACK_CHANNEL_CRITICAL
    return settings.SLACK_CHANNEL_ALERTS
