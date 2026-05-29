"""my_chat_member / chat_member events. Phase 4.

Onboarding: when the bot is added to a group it inserts a ``status='pending'``
``chats`` row and DMs every enabled admin so they can ``/authorize`` or
``/reject`` it (CLAUDE.md 7.2). These updates are not ``Message`` objects, so they
bypass the whitelist middleware and reach this router even while the chat is
still pending (see ``middleware/whitelist.py``).

The bot never writes in the partner chat here either: the only outbound messages
are DMs to internal admins.
"""

from __future__ import annotations

from html import escape as html_escape

from aiogram import Bot, Router
from aiogram.enums import ChatType
from aiogram.exceptions import TelegramAPIError
from aiogram.filters import JOIN_TRANSITION, LEAVE_TRANSITION, ChatMemberUpdatedFilter
from aiogram.types import ChatMemberUpdated

from src.db.client import acquire_connection
from src.db.models import Chat, InternalUser
from src.db.queries.chats import create_pending_chat
from src.db.queries.etc import list_admin_users
from src.utils.logging import get_logger

log = get_logger(__name__)

router = Router(name="onboarding")

# Chat types where onboarding applies. Channels and private chats are ignored:
# the bot only monitors partner *group* chats.
_GROUP_TYPES = (ChatType.GROUP, ChatType.SUPERGROUP)


@router.my_chat_member(ChatMemberUpdatedFilter(member_status_changed=JOIN_TRANSITION))
async def on_bot_added(event: ChatMemberUpdated, bot: Bot) -> None:
    """Bot joined a chat: register it as pending and notify admins.

    ``JOIN_TRANSITION`` fires when the tracked member (here, the bot itself)
    moves from a left/kicked state into the chat. Idempotent via
    ``create_pending_chat``: a re-add of a chat we already know returns ``None``
    and we do not re-notify.
    """
    chat = event.chat
    if chat.type not in _GROUP_TYPES:
        log.debug("onboarding.skip_non_group", chat_id=chat.id, chat_type=chat.type)
        return

    actor = event.from_user
    actor_id = actor.id if actor is not None else None

    async with acquire_connection() as conn:
        created = await create_pending_chat(
            conn,
            telegram_chat_id=chat.id,
            chat_name=chat.title,
            added_by_user_id=actor_id,
        )
        if created is None:
            log.info("onboarding.already_known", chat_id=chat.id)
            return
        admins = await list_admin_users(conn)

    log.info(
        "onboarding.pending_created",
        chat_id=chat.id,
        chat_name=chat.title,
        added_by=actor_id,
    )
    await _notify_admins_new_chat(bot, admins, created)


@router.my_chat_member(ChatMemberUpdatedFilter(member_status_changed=LEAVE_TRANSITION))
async def on_bot_removed(event: ChatMemberUpdated) -> None:
    """Bot left or was removed from a chat. Logged for visibility (no DB change).

    Status reconciliation (marking the chat inactive/banned on removal) is left to
    a later phase; recording it here keeps the audit trail honest in the meantime.
    """
    log.info("onboarding.bot_removed", chat_id=event.chat.id, chat_type=event.chat.type)


async def _notify_admins_new_chat(
    bot: Bot,
    admins: list[InternalUser],
    chat: Chat,
) -> None:
    """DM every reachable admin about a chat awaiting authorization.

    A person may have several Telegram accounts but can only be DM'd on ones that
    have started the bot (Telegram restriction, CLAUDE.md 11.3). We try each
    account and stop after the first success, so each admin gets one notification;
    an admin with no reachable account is logged, not retried.
    """
    text = _format_pending_notice(chat)
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


def _format_pending_notice(chat: Chat) -> str:
    """Build the admin DM body for a newly-pending chat."""
    name = html_escape(chat.chat_name) if chat.chat_name else "<i>(untitled)</i>"
    added_by = (
        f"<code>{chat.added_by_user_id}</code>"
        if chat.added_by_user_id is not None
        else "unknown"
    )
    return (
        "🆕 <b>New chat pending authorization</b>\n\n"
        f"<b>Chat:</b> {name}\n"
        f"<b>Chat id:</b> <code>{chat.telegram_chat_id}</code>\n"
        f"<b>Added by:</b> {added_by}\n\n"
        "Authorize:  "
        f"<code>/authorize {chat.telegram_chat_id} &lt;partner name&gt;</code>\n"
        f"Reject:  <code>/reject {chat.telegram_chat_id}</code>\n\n"
        "Until authorized, I store nothing from this chat."
    )
