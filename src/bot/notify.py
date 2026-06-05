"""Admin DM notifications for newly-pending monitored units.

Shared by the onboarding handler (bot added to a supergroup -> group-level unit)
and the whitelist middleware (a message in a not-yet-known forum topic -> topic
unit discovered). Kept here so a middleware never has to import a handler.

The bot never writes in the partner chat: these are DMs to internal admins only.
"""

from __future__ import annotations

from html import escape as html_escape

from aiogram import Bot
from aiogram.exceptions import TelegramAPIError

from src.db.models import Chat, InternalUser
from src.utils.logging import get_logger

log = get_logger(__name__)


def format_pending_notice(chat: Chat) -> str:
    """Build the admin DM body for a newly-pending unit (group or forum topic).

    The suggested commands differ by unit type: a group uses the group-level
    ``/authorize`` / ``/reject``; a forum topic uses ``/authorize_topic`` /
    ``/reject_topic`` (which carry the thread id and never make the bot leave).
    """
    thread = chat.message_thread_id or 0
    name = html_escape(chat.chat_name) if chat.chat_name else "<i>(untitled)</i>"
    added_by = (
        f"<code>{chat.added_by_user_id}</code>"
        if chat.added_by_user_id is not None
        else "unknown"
    )

    if chat.unit_type == "topic":
        topic = (
            f"{html_escape(chat.topic_name)} (thread <code>{thread}</code>)"
            if chat.topic_name
            else f"thread <code>{thread}</code>"
        )
        header = "📂 <b>New topic pending authorization</b>"
        authorize = (
            f"<code>/authorize_topic {chat.telegram_chat_id} {thread} "
            "&lt;partner name&gt;</code>"
        )
        reject = f"<code>/reject_topic {chat.telegram_chat_id} {thread}</code>"
    else:
        topic = "whole group / General"
        header = "🆕 <b>New group pending authorization</b>"
        authorize = (
            f"<code>/authorize {chat.telegram_chat_id} &lt;partner name&gt;</code>"
        )
        reject = f"<code>/reject {chat.telegram_chat_id}</code>"

    return (
        f"{header}\n\n"
        f"<b>Chat:</b> {name}\n"
        f"<b>Chat id:</b> <code>{chat.telegram_chat_id}</code>\n"
        f"<b>Topic:</b> {topic}\n"
        f"<b>Added by:</b> {added_by}\n\n"
        f"Authorize:  {authorize}\n"
        f"Reject:  {reject}\n\n"
        "Until authorized, I store nothing from this unit."
    )


def format_auto_active_notice(chat: Chat, adder: InternalUser) -> str:
    """Admin FYI DM: a trusted internal user connected a chat (now auto-active).

    Sent when a known internal user adds the bot to a group — no approval is
    needed (we trust the verified user, not each chat), so this is an oversight
    notice, not an action request. It nudges the admin to attach a partner and
    points at the panel. The adder themselves is *not* DM'd (cover posture).
    """
    name = html_escape(chat.chat_name) if chat.chat_name else "<i>(untitled)</i>"
    return (
        "✅ <b>New chat auto-activated</b>\n\n"
        f"<b>Chat:</b> {name}\n"
        f"<b>Chat id:</b> <code>{chat.telegram_chat_id}</code>\n"
        f"<b>Connected by:</b> {html_escape(adder.full_name)} "
        f"(role={html_escape(adder.role)})\n\n"
        "Monitoring is live. Bind a partner:\n"
        f'<code>/bind_partner {chat.telegram_chat_id} "Partner Name"</code>\n'
        "Review all connections: /admin"
    )


async def notify_internal_user(bot: Bot, user: InternalUser, text: str) -> bool:
    """DM one internal user; return whether delivery succeeded.

    A person may have several Telegram accounts but can only be DM'd on ones that
    have started the bot (Telegram restriction, CLAUDE.md 11.3). We try each
    account and stop after the first success, so the user gets one message; a user
    with no reachable account is logged (not retried) and ``False`` is returned.
    """
    for account_id in user.telegram_accounts:
        try:
            await bot.send_message(account_id, text)
            return True
        except TelegramAPIError as exc:
            # Most commonly: the user has not started the bot, or blocked it.
            log.debug(
                "notify.dm_failed",
                user=user.full_name,
                account_id=account_id,
                error=str(exc),
            )
    log.warning("notify.user_unreachable", user=user.full_name)
    return False


async def notify_admins(bot: Bot, admins: list[InternalUser], text: str) -> None:
    """DM every reachable admin a free-text message (one notification each)."""
    for admin in admins:
        await notify_internal_user(bot, admin, text)


async def notify_admins_pending(
    bot: Bot, admins: list[InternalUser], chat: Chat
) -> None:
    """DM every reachable admin about a unit awaiting authorization."""
    await notify_admins(bot, admins, format_pending_notice(chat))
