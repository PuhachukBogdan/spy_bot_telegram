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


def format_auto_active_notice(
    chat: Chat, adder: InternalUser, *, partner_name: str | None = None
) -> str:
    """Admin FYI DM: a trusted internal user connected a chat (now auto-active).

    Sent when a known internal user adds the bot to a group — no approval is
    needed (we trust the verified user, not each chat), so this is an oversight
    notice, not an action request. When ``partner_name`` is given the partner was
    auto-bound from the chat title; otherwise the admin is nudged to do it manually.
    The adder themselves is *not* DM'd (cover posture).
    """
    name = html_escape(chat.chat_name) if chat.chat_name else "<i>(untitled)</i>"
    if partner_name:
        partner_line = f"Partner <b>{html_escape(partner_name)}</b> auto-bound from chat title."
    else:
        partner_line = (
            "Monitoring is live. Bind a partner:\n"
            f'<code>/bind_partner {chat.telegram_chat_id} "Partner Name"</code>'
        )
    return (
        "✅ <b>New chat auto-activated</b>\n\n"
        f"<b>Chat:</b> {name}\n"
        f"<b>Chat id:</b> <code>{chat.telegram_chat_id}</code>\n"
        f"<b>Connected by:</b> {html_escape(adder.full_name)} "
        f"(role={html_escape(adder.role)})\n\n"
        f"{partner_line}\n"
        "Review all connections: /admin"
    )


async def pin_replacing_previous(bot: Bot, chat_id: int, message_id: int) -> bool:
    """Pin a message in a DM, unpinning the bot's own previous pin there.

    What "previous" means is read from Telegram (``get_chat().pinned_message``)
    rather than from a stored id: one call, no table, and it stays right when
    someone unpins by hand or the bot is redeployed with a cold database. Only a
    pin the BOT authored is removed — a message the reader pinned themselves is
    theirs, not ours to touch.

    Best-effort like every other DM here. Pinning is a convenience on top of a
    message that has already been delivered, so a chat where it is not allowed
    costs a log line, never the delivery.
    """
    try:
        me = await bot.me()
        previous = (await bot.get_chat(chat_id)).pinned_message
        if (
            previous is not None
            and previous.message_id != message_id
            and previous.from_user is not None
            and previous.from_user.id == me.id
        ):
            await bot.unpin_chat_message(chat_id=chat_id, message_id=previous.message_id)
        # Silent: the message itself already notified, and a second ping for its
        # pin is the kind of noise that gets a bot muted.
        await bot.pin_chat_message(
            chat_id=chat_id, message_id=message_id, disable_notification=True
        )
        return True
    except TelegramAPIError as exc:
        log.warning("notify.pin_failed", chat_id=chat_id, error=str(exc))
        return False


async def notify_internal_user(
    bot: Bot, user: InternalUser, text: str, *, pin: bool = False
) -> bool:
    """DM one internal user; return whether delivery succeeded.

    A person may have several Telegram accounts but can only be DM'd on ones that
    have started the bot (Telegram restriction, CLAUDE.md 11.3). We try each
    account and stop after the first success, so the user gets one message; a user
    with no reachable account is logged (not retried) and ``False`` is returned.

    ``pin`` replaces the bot's previous pin in that chat with this message — how
    the weekly report keeps one live link at the top of the conversation.
    """
    for account_id in user.telegram_accounts:
        try:
            sent = await bot.send_message(account_id, text)
            if pin:
                await pin_replacing_previous(bot, account_id, sent.message_id)
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


async def notify_telegram_id(
    bot: Bot, chat_id: int, text: str, *, pin: bool = False
) -> bool:
    """DM a raw Telegram id; return whether delivery succeeded.

    The counterpart of :func:`notify_internal_user` for someone the bot knows only
    by id — a report reader seeded in ``.env`` who has no ``internal_users`` row
    to carry their accounts. Same best-effort contract: a person who never pressed
    Start (Telegram refuses to let a bot open that conversation) is logged once
    and never retried, because a release must not fail over one unreachable
    reader.
    """
    try:
        sent = await bot.send_message(chat_id, text)
        if pin:
            await pin_replacing_previous(bot, chat_id, sent.message_id)
        return True
    except TelegramAPIError as exc:
        log.warning("notify.chat_unreachable", chat_id=chat_id, error=str(exc))
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
