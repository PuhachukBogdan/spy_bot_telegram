"""my_chat_member / chat_member events. Phase 4 (trusted-adder model since Phase 13).

Onboarding trusts the *user*, not the chat. The bot is added to a group by one of
our people; rather than approve every chat, we look at *who* added it:

  * A known, enabled internal user (any role) → the unit is created
    ``status='active'`` immediately (``create_active_chat``) and monitoring starts.
    No partner is bound yet; an admin attaches one later with ``/bind_partner`` and
    oversees everything via ``/admin``. The adder is trusted because they passed
    verification once when they were added to ``internal_users``.
  * An unknown / external adder → the legacy path: a ``status='pending'`` unit and
    a DM to every admin to ``/authorize`` or ``/reject`` (CLAUDE.md 7.2).

These updates are not ``Message`` objects, so they bypass the whitelist
middleware and reach this router even while a chat is still pending (see
``middleware/whitelist.py``).

The bot never writes in the partner chat here either: the only outbound messages
are DMs to internal admins. The user who added the bot is deliberately NOT DM'd —
that would reveal the monitoring layer to a manager (cover posture).
"""

from __future__ import annotations

from aiogram import Bot, Router
from aiogram.enums import ChatType
from aiogram.filters import JOIN_TRANSITION, LEAVE_TRANSITION, ChatMemberUpdatedFilter
from aiogram.types import ChatMemberUpdated

from src.bot.notify import format_auto_active_notice, notify_admins, notify_admins_pending
from src.db.client import acquire_connection
from src.db.queries.audit import insert_audit_log
from src.db.queries.chats import create_active_chat, create_pending_chat
from src.db.queries.etc import find_internal_user_by_telegram_id, list_admin_users
from src.utils.logging import get_logger

log = get_logger(__name__)

router = Router(name="onboarding")

# Chat types where onboarding applies. Channels and private chats are ignored:
# the bot only monitors partner *group* chats.
_GROUP_TYPES = (ChatType.GROUP, ChatType.SUPERGROUP)


@router.my_chat_member(ChatMemberUpdatedFilter(member_status_changed=JOIN_TRANSITION))
async def on_bot_added(event: ChatMemberUpdated, bot: Bot) -> None:
    """Bot joined a chat: auto-activate for a trusted adder, else register pending.

    ``JOIN_TRANSITION`` fires when the tracked member (here, the bot itself) moves
    from a left/kicked state into the chat. We branch on *who* added the bot:

      * known enabled internal user → ``create_active_chat`` (live immediately);
        admins get an FYI DM with the ``/bind_partner`` nudge.
      * unknown / external adder → ``create_pending_chat`` + the legacy
        authorize/reject admin DM.

    Idempotent: a re-add of a unit we already track returns ``None`` from either
    create and we do not re-notify.
    """
    chat = event.chat
    if chat.type not in _GROUP_TYPES:
        log.debug("onboarding.skip_non_group", chat_id=chat.id, chat_type=chat.type)
        return

    actor = event.from_user
    actor_id = actor.id if actor is not None else None

    # Bot-add gives no per-topic event, so this registers the group-level unit
    # (thread None). Individual forum topics are discovered lazily from their
    # first message by the whitelist middleware.
    async with acquire_connection() as conn:
        adder = (
            await find_internal_user_by_telegram_id(conn, actor_id)
            if actor_id is not None
            else None
        )
        if adder is not None:
            created = await create_active_chat(
                conn,
                telegram_chat_id=chat.id,
                thread_id=None,
                chat_name=chat.title,
                added_by_user_id=actor_id,
                authorized_by=adder.id,
            )
            if created is None:
                log.info("onboarding.already_known", chat_id=chat.id)
                return
            await insert_audit_log(
                conn,
                action="chat_auto_activated",
                actor_user_id=actor_id,
                actor_internal_id=adder.id,
                target_entity="chat",
                target_id=created.id,
                payload={"telegram_chat_id": chat.id, "role": adder.role},
            )
        else:
            created = await create_pending_chat(
                conn,
                telegram_chat_id=chat.id,
                thread_id=None,
                chat_name=chat.title,
                added_by_user_id=actor_id,
            )
            if created is None:
                log.info("onboarding.already_known", chat_id=chat.id)
                return
        admins = await list_admin_users(conn)

    if adder is not None:
        log.info(
            "onboarding.auto_activated",
            chat_id=chat.id,
            chat_name=chat.title,
            by=adder.full_name,
            role=adder.role,
        )
        # FYI to admins; the adder is intentionally not notified (cover posture).
        await notify_admins(bot, admins, format_auto_active_notice(created, adder))
    else:
        log.info(
            "onboarding.pending_created",
            chat_id=chat.id,
            chat_name=chat.title,
            added_by=actor_id,
        )
        await notify_admins_pending(bot, admins, created)


@router.my_chat_member(ChatMemberUpdatedFilter(member_status_changed=LEAVE_TRANSITION))
async def on_bot_removed(event: ChatMemberUpdated) -> None:
    """Bot left or was removed from a chat. Logged for visibility (no DB change).

    Status reconciliation (marking the chat inactive/banned on removal) is left to
    a later phase; recording it here keeps the audit trail honest in the meantime.
    """
    log.info("onboarding.bot_removed", chat_id=event.chat.id, chat_type=event.chat.type)
