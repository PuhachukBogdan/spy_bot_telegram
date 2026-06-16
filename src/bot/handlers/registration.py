"""/register — Slack identity linking via OTP. Phase 17.

Any internal user (admin / manager / viewer) can link their Slack account.
The flow is a two-step in-DM conversation:

  1. /register           → bot asks for Slack user ID
  2. user sends ID       → bot sends OTP to Slack; awaits code in Telegram
  3. user sends OTP      → bot stores slack_user_id; done

State lives in two module-level dicts (in-memory, TTL = 10 min). Lost on
restart, which is acceptable: the whole handshake takes under a minute in
practice and nothing is left uncommitted if it drops.

Cover: the command and all prompts are framed as "receive notifications" —
no hint of monitoring or risk scoring.
"""

from __future__ import annotations

import re
import secrets
import time
from dataclasses import dataclass, field
from html import escape as html_escape
from typing import Any
from uuid import UUID

from aiogram import F, Router
from aiogram.enums import ChatType
from aiogram.filters import Command
from aiogram.types import Message

from src.alerts.slack import SlackDeliveryError, send_dm_to_user
from src.bot.middleware.roles import require_role
from src.db.client import acquire_connection
from src.db.models import InternalUser
from src.db.queries.audit import insert_audit_log
from src.db.queries.etc import update_slack_user_id
from src.utils.logging import get_logger

log = get_logger(__name__)

router = Router(name="registration")
router.message.filter(F.chat.type == ChatType.PRIVATE)

_TTL_SECONDS = 600  # 10 minutes

# Slack member IDs: U + 8-12 uppercase alphanumeric chars (W for Enterprise Grid).
_SLACK_ID_RE = re.compile(r"^[UW][A-Z0-9]{8,12}$")


@dataclass
class _PendingReg:
    internal_user_id: UUID
    slack_user_id: str
    code: str
    expires_at: float = field(default_factory=lambda: time.monotonic() + _TTL_SECONDS)

    def is_expired(self) -> bool:
        return time.monotonic() > self.expires_at


# tg_user_id → internal UUID (step 1: waiting for Slack ID input)
_awaiting_slack_id: dict[int, UUID] = {}
# tg_user_id → full pending state (step 2: OTP sent, waiting for code)
_pending: dict[int, _PendingReg] = {}


def _cleanup(tg_user_id: int) -> None:
    _awaiting_slack_id.pop(tg_user_id, None)
    _pending.pop(tg_user_id, None)


@router.message(Command("register"))
@require_role("admin", "manager", "viewer")
async def cmd_register(message: Message, actor: InternalUser, **kwargs: Any) -> None:
    """Start (or restart) the Slack account linking flow (any internal user)."""
    user_id = message.from_user.id if message.from_user else None
    if user_id is None:
        return

    _cleanup(user_id)
    _awaiting_slack_id[user_id] = actor.id

    already = " (replacing the current link)" if actor.slack_user_id else ""
    await message.answer(
        f"<b>Link your Slack account{already}</b>\n\n"
        "Send me your Slack member ID to receive notifications.\n\n"
        "<b>How to find it:</b> open Slack → click your avatar → "
        "<b>View Profile</b> → <b>More (…)</b> → <b>Copy member ID</b>\n\n"
        "It looks like: <code>U01234ABCDE</code>\n\n"
        "Send the ID now, or /cancel to stop."
    )


@router.message(Command("cancel"))
async def cmd_cancel(message: Message) -> None:
    """Cancel an in-progress /register flow (silently ignored if none active)."""
    user_id = message.from_user.id if message.from_user else None
    if user_id is None:
        return
    if user_id in _awaiting_slack_id or user_id in _pending:
        _cleanup(user_id)
        await message.answer("Registration cancelled.")


@router.message(F.text, ~F.text.startswith("/"))
async def handle_registration_text(message: Message) -> None:
    """Route plain-text DM input to the active /register step, if any."""
    user_id = message.from_user.id if message.from_user else None
    text = (message.text or "").strip()
    if user_id is None or not text:
        return

    if user_id in _awaiting_slack_id:
        await _step_receive_slack_id(message, user_id, text)
    elif user_id in _pending:
        await _step_receive_code(message, user_id, text)
    # Not in a registration flow: silently do nothing.


async def _step_receive_slack_id(message: Message, user_id: int, text: str) -> None:
    """Step 1 → 2: validate Slack ID, send OTP, advance state."""
    slack_id = text.upper()
    if not _SLACK_ID_RE.match(slack_id):
        await message.answer(
            "That doesn't look like a Slack member ID.\n\n"
            "It should start with <code>U</code> and contain 8–12 uppercase "
            "letters and digits.\n"
            "Example: <code>U01234ABCDE</code>\n\n"
            "Try again or send /cancel to stop."
        )
        return

    internal_id = _awaiting_slack_id.get(user_id)
    if internal_id is None:
        return

    code = secrets.token_hex(3).upper()  # 6 hex chars — easy to copy/type
    try:
        await send_dm_to_user(
            slack_id,
            f"Your Partner Assistant verification code: *{code}*\n\n"
            "Enter this code in Telegram to link your account. "
            "It expires in 10 minutes.",
        )
    except SlackDeliveryError as exc:
        log.warning("registration.slack_dm_failed", slack_id=slack_id, error=str(exc))
        await message.answer(
            "Couldn't send a message to that Slack account. "
            "Check the ID and try again, or send /cancel to stop.\n\n"
            f"<i>Error: {html_escape(str(exc)[:120])}</i>"
        )
        return

    _awaiting_slack_id.pop(user_id, None)
    _pending[user_id] = _PendingReg(
        internal_user_id=internal_id,
        slack_user_id=slack_id,
        code=code,
    )
    log.info("registration.otp_sent", tg_user_id=user_id, slack_id=slack_id)
    await message.answer(
        "A 6-character code has been sent to your Slack DM.\n\n"
        "Enter it here to finish linking:"
    )


async def _step_receive_code(message: Message, user_id: int, text: str) -> None:
    """Step 2 → done: verify OTP, store slack_user_id."""
    pending = _pending.get(user_id)
    if pending is None or pending.is_expired():
        _cleanup(user_id)
        await message.answer(
            "Your verification session expired. Send /register to start again."
        )
        return

    if text.strip().upper() != pending.code:
        await message.answer(
            "That code doesn't match. Check for typos and try again, "
            "or send /register to restart."
        )
        return

    async with acquire_connection() as conn:
        async with conn.transaction():
            updated = await update_slack_user_id(
                conn, pending.internal_user_id, pending.slack_user_id
            )
            if updated is None:
                await message.answer("Something went wrong. Please try again.")
                _cleanup(user_id)
                return
            await insert_audit_log(
                conn,
                action="register_slack",
                actor_user_id=user_id,
                actor_internal_id=pending.internal_user_id,
                target_entity="internal_user",
                target_id=pending.internal_user_id,
                payload={"slack_user_id": pending.slack_user_id},
            )

    _cleanup(user_id)
    log.info(
        "registration.complete",
        tg_user_id=user_id,
        internal_id=str(pending.internal_user_id),
        slack_id=pending.slack_user_id,
    )
    await message.answer(
        "✅ Your Slack account has been linked.\n\n"
        f"Slack ID: <code>{html_escape(pending.slack_user_id)}</code>"
    )
