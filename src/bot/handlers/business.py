"""Telegram Business mode — read-only secretary. Migration 0006.

HARD INVARIANT: in this mode the bot ONLY READS. It never replies through a
business connection — no ``send_message`` with a ``business_connection_id``,
ever. The only outbound messages are DMs to our own internal staff (CLAUDE.md:
the bot is silent toward partners).

These four updates ride their own dispatcher observers (``business_connection`` /
``business_message`` / ``edited_business_message`` / ``deleted_business_messages``),
NOT the ``message`` observer — so the audit + whitelist middlewares (registered on
``dp.message``) never see them. Gating therefore lives here: a business message is
processed only when its connection grant is ``status='active'`` AND it maps to an
``status='active'`` business chat unit.

A Telegram Business connection is granted by a business *account owner* — one of
our account managers — who points the bot at their own DMs with partners. So:

  * ``business_account_user_id``  → our internal staff member (the connection owner)
  * the chat peer (``chat.id``)   → the partner's contact (a ``partner_contacts`` row)

A brand-new connection is auto-activated only if the owner is an internal
``admin``; otherwise it stays ``pending`` until an admin runs ``/approve_business``.
A business message from an unknown peer creates a ``pending`` chat unit and DMs the
owner with a ``/link_business_chat`` suggestion; a known peer (``partner_contacts``)
is auto-linked and monitored immediately.

Manual test (see the end of this file).
"""

from __future__ import annotations

from datetime import UTC, datetime
from html import escape as html_escape

from aiogram import Bot, Router
from aiogram.types import BusinessConnection, BusinessMessagesDeleted, Message

from src.bot.notify import notify_admins, notify_internal_user
from src.config import settings
from src.db.client import acquire_connection
from src.db.models import BusinessConnection as BusinessConnectionRow
from src.db.models import Chat, InternalUser
from src.db.queries import business_connections as bc_q
from src.db.queries import partner_contacts as pc_q
from src.db.queries.audit import insert_audit_log
from src.db.queries.chats import create_business_chat, get_by_unit
from src.db.queries.etc import (
    find_internal_user_by_telegram_id,
    get_internal_user_by_id,
    list_admin_users,
)
from src.db.queries.messages import (
    bump_message_for_analysis,
    get_message,
    insert_message_edit,
    mark_message_deleted,
    update_message_text,
    update_message_triggers,
)
from src.db.queries.queue import enqueue_chat_analysis
from src.pipeline.ingest import ingest_message
from src.pipeline.tier1 import pattern_cache
from src.utils.logging import get_logger

log = get_logger(__name__)

router = Router(name="business")


# =============================================================================
# business_connection — the bot is added to / removed from a business account.
# =============================================================================
@router.business_connection()
async def on_business_connection(update: BusinessConnection, bot: Bot) -> None:
    """Record or update a Business connection grant (always — it's a system update).

    New grant: classify the owner. An internal admin auto-activates; anyone else
    (internal non-admin, or an external account) lands ``pending`` and admins are
    DM'd to ``/approve_business``. An existing grant is refreshed; if Telegram says
    it's no longer enabled, it's marked ``revoked``.
    """
    bc_id = update.id
    owner_user_id = update.user.id
    rights = update.rights.model_dump(mode="json") if update.rights is not None else {}
    raw = update.model_dump(mode="json", exclude_none=True)

    notify: tuple[list[InternalUser], str] | None = None

    async with acquire_connection() as conn:
        existing = await bc_q.get_by_connection_id(conn, bc_id)
        if existing is not None:
            if not update.is_enabled:
                new_status = "revoked"
                await bc_q.update_status(
                    conn, bc_id, new_status, rights=rights, revoked_at=datetime.now(UTC)
                )
            else:
                new_status = existing.status  # keep pending/active; just refresh rights
                await bc_q.update_status(conn, bc_id, new_status, rights=rights)
            await insert_audit_log(
                conn,
                action="business_connection_updated",
                actor_user_id=owner_user_id,
                actor_internal_id=existing.internal_user_id,
                target_entity="business_connection",
                target_id=existing.id,
                payload={"bc_id": bc_id, "status": new_status, "user_id": owner_user_id},
            )
            log.info(
                "business.connection_updated",
                bc_id=bc_id,
                status=new_status,
                enabled=update.is_enabled,
            )
            return

        internal = await find_internal_user_by_telegram_id(conn, owner_user_id)
        if internal is not None and internal.role == "admin":
            status, approved_by, approved_at = "active", internal.id, datetime.now(UTC)
        else:
            status, approved_by, approved_at = "pending", None, None

        created = await bc_q.create(
            conn,
            BusinessConnectionRow(
                business_connection_id=bc_id,
                business_account_user_id=owner_user_id,
                internal_user_id=internal.id if internal is not None else None,
                status=status,
                rights=rights,
                connected_at=update.date,
                approved_by=approved_by,
                approved_at=approved_at,
                raw_payload=raw,
            ),
        )
        await insert_audit_log(
            conn,
            action="business_connection_created",
            actor_user_id=owner_user_id,
            actor_internal_id=internal.id if internal is not None else None,
            target_entity="business_connection",
            target_id=created.id,
            payload={
                "bc_id": bc_id,
                "status": status,
                "user_id": owner_user_id,
                "internal": internal is not None,
                "role": internal.role if internal is not None else None,
            },
        )
        if status == "pending":
            admins = await list_admin_users(conn)
            notify = (admins, _connection_notice(bc_id, owner_user_id, internal))

    if notify is not None:
        admins, text = notify
        await notify_admins(bot, admins, text)
    log.info(
        "business.connection_created",
        bc_id=bc_id,
        status=status,
        internal=internal is not None,
        owner=owner_user_id,
    )


# =============================================================================
# business_message — a message in the owner's DM with a partner. Read-only.
# =============================================================================
@router.business_message()
async def on_business_message(message: Message, bot: Bot) -> None:
    """Ingest a business DM when its connection + chat unit are both active.

    Drops content when the connection is not active, the peer maps to a
    non-business unit, or the unit is pending/rejected. An unknown peer is
    auto-linked (known ``partner_contacts``) or parked pending + the owner is DM'd.
    """
    bc_id = message.business_connection_id
    if bc_id is None:  # defensive: business messages always carry one
        return
    peer_user_id = message.chat.id

    unit_to_ingest: Chat | None = None
    pending_dm: tuple[InternalUser, str] | None = None

    async with acquire_connection() as conn:
        grant = await bc_q.get_by_connection_id(conn, bc_id)
        if grant is None or grant.status != "active":
            log.debug(
                "business.drop_inactive_conn",
                bc_id=bc_id,
                status=grant.status if grant is not None else None,
            )
            return

        unit = await get_by_unit(conn, peer_user_id, 0)
        if unit is not None:
            if unit.unit_type != "business" or unit.business_connection_id != bc_id:
                log.warning(
                    "business.unit_mismatch",
                    peer_user_id=peer_user_id,
                    bc_id=bc_id,
                    unit_type=unit.unit_type,
                )
                return
            if unit.status == "active":
                unit_to_ingest = unit
            else:
                log.debug(
                    "business.drop_inactive_unit",
                    peer_user_id=peer_user_id,
                    status=unit.status,
                )
        else:
            contact = await pc_q.get_by_telegram_user_id(conn, peer_user_id)
            if contact is not None:
                unit_to_ingest = await create_business_chat(
                    conn,
                    telegram_chat_id=peer_user_id,
                    business_connection_id=bc_id,
                    business_peer_user_id=peer_user_id,
                    partner_id=contact.partner_id,
                    status="active",
                    chat_name=_peer_label(message),
                    authorized_by=grant.internal_user_id,
                )
                if unit_to_ingest is not None:
                    await insert_audit_log(
                        conn,
                        action="business_chat_auto_linked",
                        actor_internal_id=grant.internal_user_id,
                        target_entity="chat",
                        target_id=unit_to_ingest.id,
                        payload={
                            "partner_id": str(contact.partner_id),
                            "peer_user_id": peer_user_id,
                            "bc_id": bc_id,
                        },
                    )
                    log.info(
                        "business.chat_auto_linked",
                        peer_user_id=peer_user_id,
                        partner_id=str(contact.partner_id),
                    )
            else:
                created = await create_business_chat(
                    conn,
                    telegram_chat_id=peer_user_id,
                    business_connection_id=bc_id,
                    business_peer_user_id=peer_user_id,
                    partner_id=None,
                    status="pending",
                    chat_name=_peer_label(message),
                    authorized_by=None,
                )
                # Only DM the owner on the FIRST message (create returns the row);
                # later messages find the pending unit and create returns None.
                if created is not None:
                    await insert_audit_log(
                        conn,
                        action="business_chat_pending",
                        actor_internal_id=grant.internal_user_id,
                        target_entity="chat",
                        target_id=created.id,
                        payload={"peer_user_id": peer_user_id, "bc_id": bc_id},
                    )
                    owner = (
                        await get_internal_user_by_id(conn, grant.internal_user_id)
                        if grant.internal_user_id is not None
                        else None
                    )
                    if owner is not None:
                        pending_dm = (owner, _link_prompt(bc_id, peer_user_id, message))
                log.info("business.contact_unknown", peer_user_id=peer_user_id, bc_id=bc_id)

    # Outbound work AFTER releasing the connection (ingest opens its own).
    if pending_dm is not None:
        owner, text = pending_dm
        await notify_internal_user(bot, owner, text)
    if unit_to_ingest is not None:
        await ingest_message(message, unit_to_ingest)


# =============================================================================
# edited_business_message — record an edit to a previously-stored business message.
# =============================================================================
@router.edited_business_message()
async def on_edited_business_message(edited: Message) -> None:
    """Append a ``message_edits`` row, overwrite the text, re-run Tier-1 (CLAUDE.md 7.3).

    Mirrors the group edit handler, but gated on an active business connection +
    unit. An edit for a message we never stored finds no original row and is
    ignored.
    """
    bc_id = edited.business_connection_id
    if bc_id is None:
        return
    peer_user_id = edited.chat.id
    new_text = edited.text if edited.text is not None else edited.caption

    async with acquire_connection() as conn:
        grant = await bc_q.get_by_connection_id(conn, bc_id)
        if grant is None or grant.status != "active":
            return
        unit = await get_by_unit(conn, peer_user_id, 0)
        if (
            unit is None
            or unit.unit_type != "business"
            or unit.business_connection_id != bc_id
            or unit.status != "active"
        ):
            return
        existing = await get_message(conn, unit.id, edited.message_id)
        if existing is None or new_text == existing.message_text:
            return

        edited_at = (
            datetime.fromtimestamp(edited.edit_date, tz=UTC)
            if edited.edit_date is not None
            else edited.date
        )
        await insert_message_edit(
            conn,
            message_id=existing.id,
            old_text=existing.message_text,
            new_text=new_text,
            edited_at=edited_at,
        )
        await update_message_text(conn, existing.id, new_text)

        result = pattern_cache.match(new_text, existing.sender_role)
        await update_message_triggers(
            conn,
            existing.id,
            has_triggers=result.has_triggers,
            base_score=result.base_score,
            triggered_patterns=result.triggered_patterns,
        )
        threshold = settings.PRIORITY_SCORE_THRESHOLD
        if existing.base_score < threshold <= result.base_score:
            # Bump the edited message to the head of the analysis window (its
            # send-time is behind the watermark) and pull the chat's pass forward.
            await bump_message_for_analysis(conn, existing.id)
            await enqueue_chat_analysis(conn, unit.id, datetime.now(UTC))

    log.info(
        "business.edit_recorded",
        peer_user_id=peer_user_id,
        msg_id=edited.message_id,
        base_score=result.base_score,
    )


# =============================================================================
# deleted_business_messages — the partner deleted message(s) on their side.
# =============================================================================
@router.deleted_business_messages()
async def on_deleted_business_messages(deleted: BusinessMessagesDeleted) -> None:
    """Soft-delete stored messages (keep the row, stamp ``deleted_at`` + payload).

    A deletion in a partner chat can itself be a risk signal, so we never hard-
    delete. NOT auto-escalated in the MVP (a later phase decides what a deletion
    burst means). Messages we never stored (count 0) are silently skipped.
    """
    bc_id = deleted.business_connection_id
    peer_user_id = deleted.chat.id
    payload = deleted.model_dump(mode="json", exclude_none=True)

    marked = 0
    async with acquire_connection() as conn:
        grant = await bc_q.get_by_connection_id(conn, bc_id)
        if grant is None or grant.status != "active":
            return
        unit = await get_by_unit(conn, peer_user_id, 0)
        if (
            unit is None
            or unit.unit_type != "business"
            or unit.business_connection_id != bc_id
        ):
            return
        for msg_id in deleted.message_ids:
            marked += await mark_message_deleted(
                conn,
                chat_id=unit.id,
                telegram_message_id=msg_id,
                deletion_payload=payload,
            )
        await insert_audit_log(
            conn,
            action="business_messages_deleted",
            actor_internal_id=grant.internal_user_id,
            target_entity="chat",
            target_id=unit.id,
            payload={
                "bc_id": bc_id,
                "peer_user_id": peer_user_id,
                "message_ids": deleted.message_ids,
                "count": len(deleted.message_ids),
            },
        )
    log.info(
        "business.messages_deleted",
        peer_user_id=peer_user_id,
        bc_id=bc_id,
        count=len(deleted.message_ids),
        marked=marked,
    )


# --- helpers -----------------------------------------------------------------


def _peer_label(message: Message) -> str | None:
    """Display name for the partner peer (``message.chat`` is always the peer)."""
    chat = message.chat
    name = " ".join(part for part in (chat.first_name, chat.last_name) if part)
    if name:
        return name
    return f"@{chat.username}" if chat.username else None


def _connection_notice(
    bc_id: str, owner_user_id: int, internal: InternalUser | None
) -> str:
    """Admin DM for a pending Business connection (internal non-admin / external)."""
    approve = f"<code>/approve_business {html_escape(bc_id)}</code>"
    reject = f"<code>/reject_business {html_escape(bc_id)}</code>"
    if internal is not None:
        head = (
            "🔗 <b>Business connection pending</b>\n\n"
            f"Internal user <b>{html_escape(internal.full_name)}</b> "
            f"(role={html_escape(internal.role)}) connected a business account."
        )
    else:
        head = (
            "⚠️ <b>EXTERNAL business connection</b>\n\n"
            f"From user_id <code>{owner_user_id}</code> — not an internal user."
        )
    return f"{head}\nApprove: {approve}\nReject: {reject}"


def _link_prompt(bc_id: str, peer_user_id: int, message: Message) -> str:
    """Owner DM suggesting how to attach an unknown business contact to a partner."""
    chat = message.chat
    handle = f"@{html_escape(chat.username)}" if chat.username else "—"
    label = html_escape(_peer_label(message) or "unknown")
    return (
        "🔗 <b>New business contact</b>\n\n"
        f"From <b>{label}</b> ({handle}), user_id <code>{peer_user_id}</code>.\n"
        "Link to a partner:\n"
        f'<code>/link_business_chat {html_escape(bc_id)} {peer_user_id} '
        '"Partner Name"</code>'
    )


# =============================================================================
# Manual test (Telegram Business secretary mode)
# -----------------------------------------------------------------------------
# 1. On a real Telegram account (the *owner* — NOT the bot's own account) enable
#    Business mode: Settings → Business → Chatbots, pick this bot, grant
#    "read messages".
# 2. A `business_connection` update arrives. If that account's id is in
#    internal_users with role='admin' → the grant is auto-`active`; otherwise it
#    is `pending` and every admin gets a DM with /approve_business <bc_id>.
#    (Approve it before continuing if it landed pending.)
# 3. From a THIRD account, message the owner's account. A `business_message`
#    update arrives:
#      - if that third account's user_id is already a partner_contacts row →
#        the chat unit is auto-linked to the partner and monitoring starts;
#      - otherwise a `pending` business unit is created and the owner is DM'd a
#        /link_business_chat suggestion; content is dropped until linked.
# 4. After /link_business_chat (or auto-link), further messages are ingested
#    (source='business'); editing one records a message_edits row; deleting one
#    stamps messages.deleted_at + deletion_payload.
# 5. Turn the bot off in Business settings → a `business_connection` update with
#    is_enabled=false arrives → the grant is marked `revoked` and further
#    business messages are dropped.
# =============================================================================
