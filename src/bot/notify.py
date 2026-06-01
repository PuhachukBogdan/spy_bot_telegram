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
    """Build the admin DM body for a newly-pending unit (group or forum topic)."""
    thread = chat.message_thread_id or 0
    name = html_escape(chat.chat_name) if chat.chat_name else "<i>(untitled)</i>"
    if chat.topic_name:
        topic = f"{html_escape(chat.topic_name)} (thread <code>{thread}</code>)"
    elif thread:
        topic = f"thread <code>{thread}</code>"
    else:
        topic = "whole group / General"
    added_by = (
        f"<code>{chat.added_by_user_id}</code>"
        if chat.added_by_user_id is not None
        else "unknown"
    )
    return (
        "🆕 <b>New unit pending authorization</b>\n\n"
        f"<b>Chat:</b> {name}\n"
        f"<b>Chat id:</b> <code>{chat.telegram_chat_id}</code>\n"
        f"<b>Topic:</b> {topic}\n"
        f"<b>Added by:</b> {added_by}\n\n"
        "Authorize:  "
        f"<code>/authorize {chat.telegram_chat_id} {thread} &lt;partner name&gt;</code>\n"
        f"Reject:  <code>/reject {chat.telegram_chat_id} {thread}</code>\n\n"
        "Until authorized, I store nothing from this unit."
    )


async def notify_admins_pending(
    bot: Bot, admins: list[InternalUser], chat: Chat
) -> None:
    """DM every reachable admin about a unit awaiting authorization.

    A person may have several Telegram accounts but can only be DM'd on ones that
    have started the bot (Telegram restriction, CLAUDE.md 11.3). We try each
    account and stop after the first success, so each admin gets one notification;
    an admin with no reachable account is logged, not retried.
    """
    text = format_pending_notice(chat)
    for admin in admins:
        delivered = False
        for account_id in admin.telegram_accounts:
            try:
                await bot.send_message(account_id, text)
                delivered = True
                break
            except TelegramAPIError as exc:
                # Most commonly: admin has not started the bot, or blocked it.
                log.debug(
                    "onboarding.admin_dm_failed",
                    admin=admin.full_name,
                    account_id=account_id,
                    error=str(exc),
                )
        if not delivered:
            log.warning("onboarding.admin_unreachable", admin=admin.full_name)
