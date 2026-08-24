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

import re
from uuid import UUID

import asyncpg
from aiogram import Bot, Router
from aiogram.enums import ChatType
from aiogram.filters import JOIN_TRANSITION, LEAVE_TRANSITION, ChatMemberUpdatedFilter
from aiogram.types import ChatMemberUpdated

from src.bot.notify import format_auto_active_notice, notify_admins, notify_admins_pending
from src.db.client import acquire_connection
from src.db.queries.archive import (
    attach_archived_history,
    find_archived_unit_for_aff_ids,
)
from src.db.queries.audit import insert_audit_log
from src.db.queries.chats import bind_partner_to_chat, create_active_chat, create_pending_chat
from src.db.queries.etc import (
    find_internal_user_by_telegram_id,
    get_or_create_manager_by_aff_id,
    list_admin_users,
)
from src.db.queries.partners import get_or_create_partner_with_owner
from src.utils.logging import get_logger

log = get_logger(__name__)

router = Router(name="onboarding")

# Chat title format: pipe-separated, e.g. "{aff_id} | {partner_name} | Beton.Win".
# The "Beton.Win" company token is the required partner-chat marker; it may sit in
# any position and is discarded. The aff_id is the numeric token (any position),
# maps to the owner manager (internal_users.aff_id), and is optional. Whatever
# remains is the partner name.
_BRAND_RE = re.compile(r"beton\.?win", re.IGNORECASE)

# Every 4–6 digit id in a title, not just the first. `_parse_chat_title` keeps only
# the leading one (that is all the owner-manager lookup needs), but a chat can serve
# several affiliates — "LEGENDS | Betonwin | 58329 | 71862 | 74849" — and its
# imported history may be filed under any of them.
_AFF_ID_RE = re.compile(r"\b(\d{4,6})\b")


def _title_aff_ids(title: str | None) -> list[str]:
    """All affiliate ids in a chat title, in order, de-duplicated."""
    return list(dict.fromkeys(_AFF_ID_RE.findall(title or "")))


async def _attach_archive_history(
    conn: asyncpg.Connection, title: str | None, chat_id: UUID, actor_id: int | None
) -> None:
    """Move any imported history for this title onto the freshly-created unit.

    Best-effort by design: the bot has just been added to a partner chat and that
    must succeed whether or not an archive happens to exist. A failure here is
    logged and swallowed, because losing the onboarding over a history merge would
    be a far worse outcome than a chat that starts without its backlog — the merge
    is idempotent and can simply be re-run.
    """
    aff_ids = _title_aff_ids(title)
    if not aff_ids:
        return
    try:
        unit = await find_archived_unit_for_aff_ids(conn, aff_ids)
        if unit is None:
            return
        result = await attach_archived_history(
            conn, source_chat_id=unit.id, target_chat_id=chat_id
        )
        await insert_audit_log(
            conn,
            action="archive_history_attached",
            actor_user_id=actor_id,
            target_entity="chat",
            target_id=chat_id,
            payload={
                "from_archive_unit": str(unit.id),
                "import_aff_id": unit.import_aff_id,
                "messages_moved": result.messages_moved,
                "messages_left": result.messages_left,
                "events_moved": result.events_moved,
            },
        )
        log.info(
            "onboarding.archive_attached",
            chat_id=str(chat_id),
            aff_id=unit.import_aff_id,
            moved=result.messages_moved,
            left=result.messages_left,
        )
    except asyncpg.PostgresError as exc:
        log.warning(
            "onboarding.archive_attach_failed",
            chat_id=str(chat_id),
            aff_ids=aff_ids,
            error=str(exc),
        )


def _parse_chat_title(title: str | None) -> tuple[str, str] | None:
    """Parse a partner-chat title → (aff_id, partner_name), or None.

    Requires a "Beton.Win" token somewhere. The aff_id is the first purely-numeric
    token in any position ("" if none); the company token is dropped; the rest is
    the partner name. Returns None if there is no brand marker or no name left.
    """
    if not title:
        return None
    parts = [p.strip() for p in title.split("|") if p.strip()]
    if not any(_BRAND_RE.fullmatch(p) for p in parts):
        return None
    rest = [p for p in parts if not _BRAND_RE.fullmatch(p)]
    aff_id = ""
    name_parts: list[str] = []
    for p in rest:
        if not aff_id and p.isdigit():
            aff_id = p
        else:
            name_parts.append(p)
    name = " | ".join(name_parts).strip()
    if not name:
        return None
    return aff_id, name

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
        partner_name: str | None = None
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
            parsed = _parse_chat_title(chat.title)
            if parsed:
                aff_id, pname = parsed
                # aff_id present → derive/attach the owning manager (stub row if
                # not yet registered); "" → leave the partner unowned as before.
                owner = (
                    await get_or_create_manager_by_aff_id(conn, aff_id)
                    if aff_id
                    else None
                )
                partner = await get_or_create_partner_with_owner(
                    conn, pname, owner.id if owner else None
                )
                await bind_partner_to_chat(
                    conn,
                    telegram_chat_id=chat.id,
                    thread_id=None,
                    partner_id=partner.id,
                )
                partner_name = pname
                log.info(
                    "onboarding.partner_auto_bound",
                    chat_id=chat.id,
                    partner=pname,
                    aff_id=aff_id,
                    manager=owner.full_name if owner else None,
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

        # Whether the unit went active or pending, its imported backlog belongs to
        # it now — otherwise the chat starts empty and months of context stay
        # stranded in a placeholder no report reads.
        await _attach_archive_history(conn, chat.title, created.id, actor_id)

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
        notice = format_auto_active_notice(created, adder, partner_name=partner_name)
        await notify_admins(bot, admins, notice)
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
