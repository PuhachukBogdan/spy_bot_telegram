"""/start, /authorize, /pending, /partners, etc. Phase 3/4/13.

Phase 3 ships the read-only identity commands: ``/start``, ``/help``, ``/whoami``.
Phase 4 adds the onboarding-control commands; the partner/risk commands land in
Phase 13.

Access control (migration 0007) is enforced by the decorators in
``src.bot.middleware.roles``:

  * ``/start``, ``/help``, ``/whoami`` — open to everyone, but only an *admin*
    sees the real bot. Managers, viewers and outsiders all get the same neutral
    "cover" responses that never hint the bot reads messages or tracks risk; only
    an admin's ``/start`` / ``/help`` / ``/whoami`` expose the monitoring purpose,
    command surface and role.
  * Onboarding — ``/pending``, ``/authorize``, ``/reject``, ``/authorize_topic``,
    ``/reject_topic`` — are ``@require_role('admin')``. A non-internal caller gets
    a neutral "Command not found."; a known non-admin gets "Insufficient
    permissions." plus an audit row. The resolved admin is injected as ``actor``.

Onboarding is split by unit type (a ``chats`` row is a monitored *unit* =
``(telegram_chat_id, topic)``):

  * Groups — ``/authorize <chat_id> <partner>`` and ``/reject <chat_id>``.
    Rejecting bans the group and makes the bot leave the Telegram supergroup
    (unless other live units of it remain).
  * Forum topics — ``/authorize_topic <chat_id> <thread_id> <partner>`` and
    ``/reject_topic <chat_id> <thread_id>``. Rejecting a topic sets status
    ``'rejected'`` and the bot STAYS in the supergroup for the other topics.

Every handler here is restricted to **private** chats by a router-level filter,
which structurally enforces CLAUDE.md's hard rule: the bot never writes in a
partner group chat, only in DMs with our own staff.

Manual e2e test scenario (roles + forum topics)::

    1. In Supabase Studio set your internal_users row to role='admin'.
    2. /whoami -> shows Role: admin.
    3. (optional) a second internal_users row with role='manager': /authorize
       from it must reply "Insufficient permissions." and write an
       unauthorized_command_attempt audit row.
    4. Create a Telegram supergroup with "Topics" enabled.
    5. Add the bot -> a pending GROUP unit is created; admins get a DM.
    6. /authorize <chat_id> "TestPartner"   -> group becomes active.
    7. Create a topic "Ops" and post a message in it.
    8. The first message is dropped, a pending TOPIC unit is created, and admins
       get a "📂 New topic pending" DM (only because the parent group is active).
    9. /authorize_topic <chat_id> <thread_id> "TestPartner Ops" -> topic active.
   10. /reject_topic <chat_id> <other_thread_id> -> that topic is 'rejected' and
       the bot stays in the supergroup for "Ops".
"""

from __future__ import annotations

from datetime import UTC, datetime
from html import escape as html_escape
from typing import Any

from aiogram import Bot, F, Router
from aiogram.enums import ChatType
from aiogram.filters import Command, CommandObject, CommandStart
from aiogram.types import Message

from src.bot.middleware.roles import require_partner_access, require_role
from src.config import settings
from src.db.client import acquire_connection
from src.db.models import InternalUser, Partner
from src.db.queries import business_connections as bc_q
from src.db.queries import notes as notes_q
from src.db.queries import risk_events as risk_q
from src.db.queries.audit import insert_audit_log
from src.db.queries.chats import (
    authorize_chat,
    bind_partner_to_chat,
    count_live_units,
    deactivate_chat,
    find_chats_by_ref,
    get_chat_unit,
    link_business_chat,
    list_chat_overviews_by_partner,
    list_chats_overview,
    list_pending,
    reject_chat,
    reject_topic,
)
from src.db.queries.cost import get_today as cost_get_today
from src.db.queries.cost import sum_last_7_days
from src.db.queries.etc import (
    create_internal_user,
    find_internal_user_by_identifier,
    find_internal_user_by_telegram_id,
    get_internal_user_by_id,
    get_internal_user_by_telegram_id_any,
    list_internal_users,
    set_user_enabled,
    update_work_hours,
)
from src.db.queries.messages import get_message_by_id, get_messages_around
from src.db.queries.partners import (
    get_or_create_partner,
    get_partner_by_id,
    get_partner_by_name,
    list_partners,
    update_partner_owner,
    update_partner_status,
)
from src.db.queries.patterns import load_enabled_patterns
from src.utils.logging import get_logger
from src.utils.workhours import parse_work_hours

log = get_logger(__name__)

router = Router(name="dm_commands")
# DM-only: this filter applies to every message handler on this router, so none
# of these commands can ever fire inside a partner group.
router.message.filter(F.chat.type == ChatType.PRIVATE)


# Cover identity shown to EVERYONE who is not an admin (managers, viewers and
# outsiders alike). It must never hint at what the bot does under the hood —
# message reading, risk scoring, monitoring. The real surface-level "cover"
# features land later; for now this is a neutral assistant face.
_START_COVER = (
    "<b>Partner Assistant</b>\n\n"
    "Hi! I'm an assistant for partner communication.\n\n"
    "Use /help to see what I can do."
)

# Honest intro — shown ONLY to an admin (no one else ever receives this string).
_START_ADMIN = (
    "<b>Partner Chat Risk Monitor — admin</b>\n\n"
    "You're recognized as an administrator. I monitor authorized partner chats "
    "and surface risk signals to you here in DM — never in the partner chats "
    "themselves.\n\n"
    "Use /help for the full command list."
)

# /help body for outsiders — the cover surface. Lists only the neutral commands;
# no trace of the monitoring layer.
_COVER_HELP_TEXT = (
    "<b>Partner Assistant</b>\n\n"
    "/start — about this bot\n"
    "/help — this message\n"
    "/whoami — show how I recognize you"
)

# Cover surface for a recognized internal user (manager / viewer). Same neutral
# face plus /set_hours — framed as a scheduling convenience, it reveals nothing
# about monitoring, but lets a manager record the work hours the SLA track needs.
_COVER_HELP_INTERNAL = (
    _COVER_HELP_TEXT
    + "\n/set_hours — set your working hours and timezone"
    + "\n/register — link your Slack account for notifications"
)

_HELP_COMMON = (
    "<b>Available commands</b>\n\n"
    "/start — intro and what I do\n"
    "/help — this message\n"
    "/whoami — show how I recognize you and your role\n"
    "/set_hours — set your working hours and timezone"
)

# Monitoring surface — ADMIN ONLY. A manager must never learn the bot tracks
# red flags (they may themselves be the subject of a flag), so none of this is
# shown to, or callable by, a non-admin.
_HELP_REVIEW = (
    "<b>Partners, chats &amp; risks:</b>\n"
    "/partners [filter] — partner list (active/passive/risky/inactive/all)\n"
    "/partner &lt;name&gt; — partner card\n"
    "/chats [filter] — chat/topic units\n"
    "/chat &lt;id|name&gt; — chat details + recent risks\n"
    "/risks [N|partner] — recent risk events\n"
    "/risk &lt;id&gt; — risk card + context\n"
    "/mark_fp · /mark_confirmed · /mark_escalated &lt;id&gt; — review a risk"
)

# Admin-only surface.
_HELP_ADMIN = (
    "<b>Admin only:</b>\n"
    "/admin — connected-chats panel (who linked what, drill down, ids)\n"
    '/users [all] · /add_manager &lt;id&gt; "Name" · /disable_user &lt;id|name&gt;'
    " — team access\n"
    "/pending · /authorize · /reject · /authorize_topic · /reject_topic — onboarding\n"
    '/bind_partner &lt;chat_id&gt; "Partner" — attach a partner to an active group\n'
    "/chat_delete &lt;id&gt; — disconnect a unit and leave the chat\n"
    "/set_partner_status &lt;name&gt; &lt;status&gt; — change partner status\n"
    "/set_owner &lt;partner&gt; &lt;user&gt; — assign owning manager\n"
    "/thresholds · /categories · /dictionary [cat] · /cost_status — settings\n"
    "/business_connections — list Business connection grants\n"
    "/approve_business · /reject_business &lt;conn_id&gt; — approve / disable a grant\n"
    '/link_business_chat &lt;conn_id&gt; &lt;user_id&gt; "Partner" — link an unknown DM'
)


def _help_for(role: str | None) -> str:
    """Build the /help body. Only an admin sees the real command surface.

    A recognized manager/viewer gets the cover text plus /set_hours; an outsider
    gets the bare cover. Neither variant hints that the bot reads messages or
    tracks risk.
    """
    if role == "admin":
        return "\n\n".join([_HELP_COMMON, _HELP_REVIEW, _HELP_ADMIN])
    if role in ("manager", "viewer"):
        return _COVER_HELP_INTERNAL
    return _COVER_HELP_TEXT


@router.message(CommandStart())
async def cmd_start(message: Message) -> None:
    """Greet the caller. Admins get the honest intro; everyone else the cover."""
    user = message.from_user
    internal: InternalUser | None = None
    if user is not None:
        async with acquire_connection() as conn:
            internal = await find_internal_user_by_telegram_id(conn, user.id)
    is_admin = internal is not None and internal.role == "admin"
    await message.answer(_START_ADMIN if is_admin else _START_COVER)


@router.message(Command("help"))
async def cmd_help(message: Message) -> None:
    """List commands for the caller's role; show a cover message to outsiders."""
    user = message.from_user
    internal: InternalUser | None = None
    if user is not None:
        async with acquire_connection() as conn:
            internal = await find_internal_user_by_telegram_id(conn, user.id)
    await message.answer(_help_for(internal.role if internal is not None else None))


@router.message(Command("whoami"))
async def cmd_whoami(message: Message) -> None:
    """Report identity. Admins get full details; everyone else a neutral line.

    A manager / viewer / outsider all get the same cover reply — it never exposes
    the role hierarchy or the internal-user concept, so it can't hint at the
    hidden functionality.
    """
    user = message.from_user
    if user is None:  # defensive: private messages always carry from_user
        await message.answer("You're chatting with the <b>Partner Assistant</b>.")
        return

    async with acquire_connection() as conn:
        internal = await find_internal_user_by_telegram_id(conn, user.id)

    if internal is not None and internal.role == "admin":
        await message.answer(
            "You are recognized as an <b>administrator</b>.\n\n"
            f"<b>Name:</b> {html_escape(internal.full_name)}\n"
            f"<b>Role:</b> {html_escape(internal.role)}\n"
            f"<b>Telegram id:</b> <code>{user.id}</code>"
        )
        return

    # Managers, viewers and outsiders all get the same neutral cover reply.
    await message.answer(
        "You're chatting with the <b>Partner Assistant</b>.\n"
        f"Your Telegram id: <code>{user.id}</code>"
    )


@router.message(Command("set_hours"))
@require_role("admin", "manager", "viewer")
async def cmd_set_hours(
    message: Message, actor: InternalUser, command: CommandObject, **kwargs: Any
) -> None:
    """Set the caller's own working hours + timezone (any internal user).

    Feeds the operational_sla track: a partner message left unanswered beyond the
    SLA threshold *during the owning manager's working hours* raises a flag, and
    the weekly summary rolls those up per manager. The command is framed as a
    neutral scheduling convenience — it fits the "Partner Assistant" cover and
    reveals nothing about monitoring. Outsiders get the decorator's neutral
    "Command not found." Usage: ``/set_hours 09:00-18:00 Europe/Kiev``.
    """
    parsed = parse_work_hours(command.args or "")
    if parsed is None:
        await message.answer(
            "Set your working hours and timezone so I can keep things to your "
            "shift.\n\n"
            "Usage: <code>/set_hours HH:MM-HH:MM Timezone</code>\n"
            "Example: <code>/set_hours 09:00-18:00 Europe/Kiev</code>\n"
            "Example: <code>/set_hours 08:30-17:30 Asia/Almaty</code>"
        )
        return

    async with acquire_connection() as conn:
        await update_work_hours(
            conn,
            actor.id,
            start=parsed.start,
            end=parsed.end,
            timezone=parsed.timezone,
        )
    log.info("dm.set_hours", actor=str(actor.id), timezone=parsed.timezone)
    await message.answer(
        "✅ Working hours saved: "
        f"<b>{parsed.start.strftime('%H:%M')}–{parsed.end.strftime('%H:%M')}</b> "
        f"({html_escape(parsed.timezone)})."
    )


@router.message(Command("pending"))
@require_role("admin")
async def cmd_pending(message: Message, actor: InternalUser, **kwargs: Any) -> None:
    """List units awaiting authorization, split into groups and topics (admin)."""
    async with acquire_connection() as conn:
        pending = await list_pending(conn)

    if not pending:
        await message.answer("No units are pending authorization. ✅")
        return

    groups = [c for c in pending if c.unit_type != "topic"]
    topics = [c for c in pending if c.unit_type == "topic"]
    lines: list[str] = []

    if groups:
        lines.append("<b>Pending groups</b>")
        for chat in groups:
            name = html_escape(chat.chat_name) if chat.chat_name else "(untitled)"
            lines.append(
                f"• {name} — <code>{chat.telegram_chat_id}</code>\n"
                f"  <code>/authorize {chat.telegram_chat_id} &lt;partner&gt;</code>"
                f" · added by <code>{chat.added_by_user_id or '—'}</code>"
            )

    if topics:
        if lines:
            lines.append("")
        lines.append("<b>Pending topics</b>")
        for chat in topics:
            thread = chat.message_thread_id or 0
            parent = html_escape(chat.chat_name) if chat.chat_name else "(untitled)"
            label = (
                html_escape(chat.topic_name)
                if chat.topic_name
                else f"thread {thread}"
            )
            lines.append(
                f"• {label} in “{parent}” (thread <code>{thread}</code>)\n"
                f"  <code>/authorize_topic {chat.telegram_chat_id} {thread} "
                "&lt;partner&gt;</code>"
            )

    await message.answer("\n".join(lines))


@router.message(Command("authorize"))
@require_role("admin")
async def cmd_authorize(
    message: Message, actor: InternalUser, command: CommandObject, **kwargs: Any
) -> None:
    """Activate a pending GROUP and bind it to a partner (admin only).

    Usage: ``/authorize <chat_id> <partner name>``. The partner is created if it
    does not exist. The activation + partner upsert + audit row commit together.
    For forum topics use ``/authorize_topic`` instead.
    """
    parsed = _parse_authorize_args(command.args)
    if parsed is None:
        await message.answer(
            "Usage: <code>/authorize &lt;chat_id&gt; &lt;partner name&gt;</code>\n"
            "Example: <code>/authorize -1001234567890 Acme Corp</code>\n"
            "(for a forum topic use <code>/authorize_topic</code>)"
        )
        return
    telegram_chat_id, partner_name = parsed

    async with acquire_connection() as conn:
        async with conn.transaction():
            chat = await get_chat_unit(conn, telegram_chat_id, None)  # group-level
            if chat is None:
                await message.answer(
                    f"I don't know group <code>{telegram_chat_id}</code>. "
                    "I only track units I've seen."
                )
                return
            if chat.status != "pending":
                await message.answer(
                    f"Group <code>{telegram_chat_id}</code> is not pending "
                    f"(current status: <b>{chat.status}</b>). Nothing to do."
                )
                return

            partner = await get_or_create_partner(conn, partner_name)
            activated = await authorize_chat(
                conn,
                telegram_chat_id=telegram_chat_id,
                thread_id=None,
                partner_id=partner.id,
                authorized_by=actor.id,
            )
            if activated is None:  # lost a race; unit left pending state
                await message.answer(
                    f"Group <code>{telegram_chat_id}</code> is no longer pending. "
                    "Nothing to do."
                )
                return

            await insert_audit_log(
                conn,
                action="authorize_chat",
                actor_user_id=message.from_user.id if message.from_user else None,
                actor_internal_id=actor.id,
                target_entity="chat",
                target_id=activated.id,
                payload={
                    "telegram_chat_id": telegram_chat_id,
                    "partner_id": str(partner.id),
                    "partner_name": partner.name,
                },
            )

    log.info(
        "onboarding.authorized",
        chat_id=telegram_chat_id,
        partner=partner.name,
        by=actor.full_name,
    )
    await message.answer(
        f"✅ Group activated and bound to partner <b>{html_escape(partner.name)}</b>. "
        "Monitoring has started."
    )


@router.message(Command("reject"))
@require_role("admin")
async def cmd_reject(
    message: Message,
    actor: InternalUser,
    command: CommandObject,
    bot: Bot,
    **kwargs: Any,
) -> None:
    """Decline a pending GROUP: mark it banned and leave it (admin only).

    Usage: ``/reject <chat_id>``. The DB flip + audit commit together; the
    ``bot.leave_chat`` call happens after commit and only when no other live unit
    of the supergroup remains (leaving would kill any monitored topics).
    """
    parsed = _parse_reject_args(command.args)
    if parsed is None:
        await message.answer(
            "Usage: <code>/reject &lt;chat_id&gt;</code>\n"
            "Example: <code>/reject -1001234567890</code>\n"
            "(for a forum topic use <code>/reject_topic</code>)"
        )
        return
    telegram_chat_id = parsed

    async with acquire_connection() as conn:
        async with conn.transaction():
            rejected = await reject_chat(conn, telegram_chat_id, None)
            if rejected is None:
                await message.answer(
                    f"Group <code>{telegram_chat_id}</code> is not pending. "
                    "Nothing to reject."
                )
                return
            await insert_audit_log(
                conn,
                action="reject_chat",
                actor_user_id=message.from_user.id if message.from_user else None,
                actor_internal_id=actor.id,
                target_entity="chat",
                target_id=rejected.id,
                payload={"telegram_chat_id": telegram_chat_id},
            )
        # Only leave the whole Telegram supergroup when no other monitored unit
        # of it remains — leaving would kill every topic.
        remaining = await count_live_units(conn, telegram_chat_id)

    if remaining == 0:
        left = await _leave_chat_quietly(bot, telegram_chat_id)
        suffix = "" if left else " (I couldn't leave it — already removed?)"
        body = f"and left the group{suffix}"
    else:
        body = f"(staying in the group — {remaining} other unit(s) still monitored)"
    log.info(
        "onboarding.rejected",
        chat_id=telegram_chat_id,
        remaining=remaining,
        by=actor.full_name,
    )
    await message.answer(
        f"🚫 Group <code>{telegram_chat_id}</code> rejected and banned {body}."
    )


@router.message(Command("authorize_topic"))
@require_role("admin")
async def cmd_authorize_topic(
    message: Message, actor: InternalUser, command: CommandObject, **kwargs: Any
) -> None:
    """Activate a pending forum TOPIC and bind it to a partner (admin only).

    Usage: ``/authorize_topic <chat_id> <thread_id> <partner name>``. Guarded to a
    unit that is still ``'pending'`` and ``unit_type='topic'`` so it can never
    flip a group-level unit.
    """
    parsed = _parse_authorize_topic_args(command.args)
    if parsed is None:
        await message.answer(
            "Usage: <code>/authorize_topic &lt;chat_id&gt; &lt;thread_id&gt; "
            "&lt;partner name&gt;</code>\n"
            'Example: <code>/authorize_topic -1001234567890 42 "Acme Ops"</code>'
        )
        return
    telegram_chat_id, thread_id, partner_name = parsed

    async with acquire_connection() as conn:
        async with conn.transaction():
            chat = await get_chat_unit(conn, telegram_chat_id, thread_id)
            if chat is None or chat.status != "pending" or chat.unit_type != "topic":
                await message.answer("No pending topic with that id.")
                return

            partner = await get_or_create_partner(conn, partner_name)
            activated = await authorize_chat(
                conn,
                telegram_chat_id=telegram_chat_id,
                thread_id=thread_id,
                partner_id=partner.id,
                authorized_by=actor.id,
            )
            if activated is None:  # lost a race
                await message.answer("No pending topic with that id.")
                return

            await insert_audit_log(
                conn,
                action="authorize_topic",
                actor_user_id=message.from_user.id if message.from_user else None,
                actor_internal_id=actor.id,
                target_entity="chat",
                target_id=activated.id,
                payload={
                    "telegram_chat_id": telegram_chat_id,
                    "thread_id": thread_id,
                    "partner_id": str(partner.id),
                    "partner_name": partner.name,
                },
            )

    log.info(
        "onboarding.topic_authorized",
        chat_id=telegram_chat_id,
        thread_id=thread_id,
        partner=partner.name,
        by=actor.full_name,
    )
    await message.answer(
        f"✅ Topic activated, monitoring started for "
        f"<b>{html_escape(partner.name)}</b>."
    )


@router.message(Command("reject_topic"))
@require_role("admin")
async def cmd_reject_topic(
    message: Message, actor: InternalUser, command: CommandObject, **kwargs: Any
) -> None:
    """Decline a pending forum TOPIC (admin only); the bot stays in the chat.

    Usage: ``/reject_topic <chat_id> <thread_id>``. Sets ``status='rejected'`` and
    does NOT leave the supergroup — other topics keep being monitored.
    """
    parsed = _parse_reject_topic_args(command.args)
    if parsed is None:
        await message.answer(
            "Usage: <code>/reject_topic &lt;chat_id&gt; &lt;thread_id&gt;</code>\n"
            "Example: <code>/reject_topic -1001234567890 42</code>"
        )
        return
    telegram_chat_id, thread_id = parsed

    async with acquire_connection() as conn:
        async with conn.transaction():
            rejected = await reject_topic(conn, telegram_chat_id, thread_id)
            if rejected is None:
                await message.answer("No pending topic with that id.")
                return
            await insert_audit_log(
                conn,
                action="reject_topic",
                actor_user_id=message.from_user.id if message.from_user else None,
                actor_internal_id=actor.id,
                target_entity="chat",
                target_id=rejected.id,
                payload={"telegram_chat_id": telegram_chat_id, "thread_id": thread_id},
            )

    log.info(
        "onboarding.topic_rejected",
        chat_id=telegram_chat_id,
        thread_id=thread_id,
        by=actor.full_name,
    )
    # Deliberately no leave_chat: the bot remains for the other topics.
    await message.answer("🚫 Topic rejected. The bot stays in the chat for other topics.")


# =============================================================================
# Partner / chat / risk / settings commands (Phase 13)
#
# Access: ADMIN ONLY — all of it. The whole monitoring surface (partners, chats,
# risk events, settings) reveals that the bot tracks red flags, and a manager may
# themselves be a subject of a flag, so only an admin may see or touch it. A
# non-admin caller gets the decorator's neutral "Command not found." / wrong-role
# audit; /help never lists these for them.
#
# The owner-scoping plumbing below (owner_id filters, per-row _risk_accessible,
# require_partner_access) is now dormant — every caller is an admin — but is kept
# intact so a future *manager-safe* view can be reintroduced without rewiring the
# queries. It is not a second access path: the decorators are the gate.
#
# Every mutation writes an admin_audit_log row; access denials are audited by the
# require_role decorator (action='unauthorized_command_attempt').
# =============================================================================

_PARTNER_FILTERS = {"active", "passive", "risky", "inactive", "all"}
_PARTNER_STATUSES = {"active", "passive", "risky", "inactive"}
_CHAT_FILTERS = {
    "active",
    "pending",
    "abandoned",
    "inactive",
    "banned",
    "rejected",
    "removed",
    "all",
}
_MAX_LIST_ROWS = 20
_MAX_RISK_LIMIT = 100

# The 12 fixed risk categories (CLAUDE.md section 7.6) with a one-line gloss.
_RISK_CATEGORIES: list[tuple[str, str]] = [
    ("shadow_deal", "off-the-books arrangement between parties"),
    ("private_channel", "moving the talk to an unmonitored channel"),
    ("hidden_payment", "undisclosed or side payments"),
    ("traffic_leakage", "diverting traffic away from us"),
    ("commercial_terms", "renegotiating commercial terms off-process"),
    ("fraud_shave", "shaving / fraud on conversions or payouts"),
    ("access_risk", "risky access or credential sharing"),
    ("partner_churn", "signals the partner may leave"),
    ("payment_conflict", "disputes over payments / invoices"),
    ("reputation_risk", "reputational / PR exposure"),
    ("operational_sla", "SLA or operational breaches"),
    ("employee_behavior", "concerning internal-employee behaviour"),
]


# --- Partner management ------------------------------------------------------


@router.message(Command("partners"))
@require_role("admin")
async def cmd_partners(
    message: Message, actor: InternalUser, command: CommandObject, **kwargs: Any
) -> None:
    """List partners with activity rollups (admin only)."""
    filt = (command.args or "active").strip().lower() or "active"
    if filt not in _PARTNER_FILTERS:
        await message.answer(
            "Usage: <code>/partners [active|passive|risky|inactive|all]</code>"
        )
        return
    status = None if filt == "all" else filt
    owner_id = None if actor.role == "admin" else actor.id
    async with acquire_connection() as conn:
        rows = await list_partners(conn, status=status, owner_id=owner_id)

    if not rows:
        await message.answer(f"No partners ({filt}).")
        return
    lines = [f"<b>Partners — {filt}</b> ({len(rows)})"]
    for r in rows[:_MAX_LIST_ROWS]:
        lines.append(
            f"• <b>{html_escape(r.name)}</b> [{r.status}] — "
            f"{r.active_chats} chat(s) — last {_fmt_dt(r.last_activity)}"
        )
    if len(rows) > _MAX_LIST_ROWS:
        lines.append(
            f"\n…and {len(rows) - _MAX_LIST_ROWS} more — use /partners with a filter."
        )
    await message.answer("\n".join(lines))
    log.info("dm.partners", actor=str(actor.id), filter=filt, count=len(rows))


@router.message(Command("partner"))
@require_role("admin")
@require_partner_access()
async def cmd_partner(
    message: Message, actor: InternalUser, partner: Partner, **kwargs: Any
) -> None:
    """Show a partner card: chats + recent risks + recent general notes."""
    async with acquire_connection() as conn:
        chats = await list_chat_overviews_by_partner(conn, partner.id)
        risks = await risk_q.list_recent(conn, partner_id=partner.id, limit=5)
        notes = await notes_q.list_by_partner(conn, partner.id, note_type="general")
        owner_name = "—"
        if partner.owner_manager_id is not None:
            owner = await get_internal_user_by_id(conn, partner.owner_manager_id)
            owner_name = owner.full_name if owner is not None else "—"

    lines = [
        f"<b>{html_escape(partner.name)}</b> [{partner.status}]",
        f"Owner: {html_escape(owner_name)}",
        f"Created: {_fmt_dt(partner.created_at)}",
        f"\n<b>Chats ({len(chats)}):</b>",
    ]
    for c in chats[:10]:
        name = html_escape(c.chat_name) if c.chat_name else "(untitled)"
        lines.append(
            f"[{c.unit_type}] {name} [{c.status}] — last {_fmt_dt(c.last_activity)}"
        )
    lines.append(f"\n<b>Recent risks ({len(risks)}):</b>")
    for r in risks:
        phrase = html_escape((r.detected_phrase or "")[:50])
        lines.append(
            f"[{r.risk_level}] {r.risk_type} — {phrase} — {_fmt_dt(r.created_at)}"
        )
    if notes:
        shown = notes[:3]
        lines.append(f"\n<b>Notes ({len(shown)}):</b>")
        for n in shown:
            lines.append(f"• {html_escape(n.content[:80])}")
    await message.answer("\n".join(lines))
    log.info("dm.partner", actor=str(actor.id), partner_id=str(partner.id))


@router.message(Command("set_partner_status"))
@require_role("admin")
async def cmd_set_partner_status(
    message: Message, actor: InternalUser, command: CommandObject, **kwargs: Any
) -> None:
    """Change a partner's status (admin only); audited."""
    parsed = _split_name_last(command.args)
    if parsed is None or parsed[1].lower() not in _PARTNER_STATUSES:
        await message.answer(
            "Usage: <code>/set_partner_status &lt;name&gt; "
            "&lt;active|passive|risky|inactive&gt;</code>"
        )
        return
    name, new_status = parsed[0], parsed[1].lower()

    async with acquire_connection() as conn:
        async with conn.transaction():
            partner = await get_partner_by_name(conn, name)
            if partner is None:
                await message.answer("Partner not found.")
                return
            old = partner.status
            await update_partner_status(conn, partner.id, new_status)
            await insert_audit_log(
                conn,
                action="set_partner_status",
                actor_user_id=message.from_user.id if message.from_user else None,
                actor_internal_id=actor.id,
                target_entity="partner",
                target_id=partner.id,
                payload={"old": old, "new": new_status, "partner_id": str(partner.id)},
            )
    log.info(
        "dm.set_partner_status",
        partner_id=str(partner.id),
        old=old,
        new=new_status,
        by=str(actor.id),
    )
    await message.answer(
        f"Partner <b>{html_escape(name)}</b>: {old} → {new_status}"
    )


@router.message(Command("set_owner"))
@require_role("admin")
async def cmd_set_owner(
    message: Message, actor: InternalUser, command: CommandObject, **kwargs: Any
) -> None:
    """Assign a partner's owning manager (admin only); audited.

    The user identifier is a Telegram user id (digits) or an exact (case-
    insensitive) full name. Telegram usernames are not stored, so they can't be
    used here.
    """
    parsed = _split_name_last(command.args)
    if parsed is None:
        await message.answer(
            "Usage: <code>/set_owner &lt;partner name&gt; "
            "&lt;full name | telegram_user_id&gt;</code>"
        )
        return
    partner_name, identifier = parsed

    async with acquire_connection() as conn:
        async with conn.transaction():
            partner = await get_partner_by_name(conn, partner_name)
            if partner is None:
                await message.answer("Partner not found.")
                return
            target = await find_internal_user_by_identifier(conn, identifier)
            if target is None:
                await message.answer("User not found.")
                return
            old_owner_name = "—"
            old_owner_id = partner.owner_manager_id
            if old_owner_id is not None:
                old_owner = await get_internal_user_by_id(conn, old_owner_id)
                old_owner_name = old_owner.full_name if old_owner is not None else "—"
            await update_partner_owner(conn, partner.id, target.id)
            await insert_audit_log(
                conn,
                action="set_owner",
                actor_user_id=message.from_user.id if message.from_user else None,
                actor_internal_id=actor.id,
                target_entity="partner",
                target_id=partner.id,
                payload={
                    "partner_id": str(partner.id),
                    "old_owner": str(old_owner_id) if old_owner_id else None,
                    "new_owner": str(target.id),
                },
            )
    log.info(
        "dm.set_owner",
        partner_id=str(partner.id),
        new_owner=str(target.id),
        by=str(actor.id),
    )
    await message.answer(
        f"Partner <b>{html_escape(partner_name)}</b> owner: "
        f"{html_escape(old_owner_name)} → {html_escape(target.full_name)}"
    )


# --- Chat management ---------------------------------------------------------


@router.message(Command("chats"))
@require_role("admin")
async def cmd_chats(
    message: Message, actor: InternalUser, command: CommandObject, **kwargs: Any
) -> None:
    """List chat/topic units (admin only)."""
    filt = (command.args or "active").strip().lower() or "active"
    if filt not in _CHAT_FILTERS:
        await message.answer(
            "Usage: <code>/chats [active|pending|abandoned|inactive|banned|"
            "rejected|removed|all]</code>"
        )
        return
    status = None if filt == "all" else filt
    owner_id = None if actor.role == "admin" else actor.id
    async with acquire_connection() as conn:
        rows = await list_chats_overview(conn, status=status, owner_id=owner_id)

    if not rows:
        await message.answer(f"No chats ({filt}).")
        return
    lines = [f"<b>Chats — {filt}</b> ({len(rows)})"]
    for c in rows[:_MAX_LIST_ROWS]:
        partner = html_escape(c.partner_name) if c.partner_name else "—"
        lines.append(
            f"• <code>{c.telegram_chat_id}</code> [{c.unit_type}] — {partner} — "
            f"last {_fmt_dt(c.last_activity)}"
        )
    if len(rows) > _MAX_LIST_ROWS:
        lines.append(
            f"\n…and {len(rows) - _MAX_LIST_ROWS} more — narrow with a filter."
        )
    await message.answer("\n".join(lines))
    log.info("dm.chats", actor=str(actor.id), filter=filt, count=len(rows))


@router.message(Command("chat"))
@require_role("admin")
async def cmd_chat(
    message: Message, actor: InternalUser, command: CommandObject, **kwargs: Any
) -> None:
    """Show one chat unit's details + its recent risks (admin only)."""
    ref = (command.args or "").strip()
    if not ref:
        await message.answer("Usage: <code>/chat &lt;id | telegram_id | name&gt;</code>")
        return
    ref_int = int(ref) if ref.lstrip("-").isdigit() else None

    async with acquire_connection() as conn:
        matches = await find_chats_by_ref(conn, ref, ref_int)
        # Manager scoping: keep only units of partners this manager owns.
        if actor.role != "admin":
            owned = []
            for c in matches:
                if c.partner_id is None:
                    continue
                p = await get_partner_by_id(conn, c.partner_id)
                if p is not None and p.owner_manager_id == actor.id:
                    owned.append(c)
            matches = owned

        if not matches:
            await message.answer("Chat not found.")
            return
        if len(matches) > 1:
            lines = ["Multiple units match — pick one by its id (first 8 chars):"]
            for c in matches[:_MAX_LIST_ROWS]:
                name = html_escape(c.chat_name) if c.chat_name else "(untitled)"
                lines.append(
                    f"• <code>{str(c.id)[:8]}</code> [{c.unit_type}] {name} "
                    f"[{c.status}]"
                )
            await message.answer("\n".join(lines))
            return

        chat = matches[0]
        risks = await risk_q.list_by_chat(conn, chat.id, limit=5)
        partner_name = "—"
        if chat.partner_id is not None:
            p = await get_partner_by_id(conn, chat.partner_id)
            partner_name = p.name if p is not None else "—"

    name = html_escape(chat.chat_name) if chat.chat_name else "(untitled)"
    lines = [
        f"<b>{name}</b> [{chat.status}]",
        f"Unit: {chat.unit_type} · id <code>{str(chat.id)[:8]}</code>",
        f"Telegram id: <code>{chat.telegram_chat_id}</code>",
        f"Thread: <code>{chat.message_thread_id or 0}</code>",
        f"Partner: {html_escape(partner_name)}",
        f"\n<b>Recent risks ({len(risks)}):</b>",
    ]
    for r in risks:
        lines.append(
            f"[{str(r.id)[:8]}] [{r.risk_level}] {r.risk_type} — {_fmt_dt(r.created_at)}"
        )
    await message.answer("\n".join(lines))
    log.info("dm.chat", actor=str(actor.id), chat_id=str(chat.id))


@router.message(Command("bind_partner"))
@require_role("admin")
async def cmd_bind_partner(
    message: Message, actor: InternalUser, command: CommandObject, **kwargs: Any
) -> None:
    """Attach a partner to an already-active GROUP unit (admin only); audited.

    Companion to the trusted-adder onboarding: a unit a manager added goes live
    with no partner, and the admin binds one here. Usage:
    ``/bind_partner <chat_id> <partner name>``. The partner is created if new. For
    a forum topic, use ``/authorize_topic`` (topics still flow through discovery).
    """
    parsed = _parse_authorize_args(command.args)
    if parsed is None:
        await message.answer(
            'Usage: <code>/bind_partner &lt;chat_id&gt; "Partner Name"</code>\n'
            "Example: <code>/bind_partner -1001234567890 Acme Corp</code>\n"
            "(for a forum topic use <code>/authorize_topic</code>)"
        )
        return
    telegram_chat_id, partner_name = parsed

    async with acquire_connection() as conn:
        async with conn.transaction():
            chat = await get_chat_unit(conn, telegram_chat_id, None)  # group-level
            if chat is None or chat.status != "active":
                await message.answer(
                    f"No active group <code>{telegram_chat_id}</code>. "
                    "(I only bind units that are already live.)"
                )
                return
            partner = await get_or_create_partner(conn, partner_name)
            updated = await bind_partner_to_chat(
                conn,
                telegram_chat_id=telegram_chat_id,
                thread_id=None,
                partner_id=partner.id,
            )
            if updated is None:  # lost a race; unit left active
                await message.answer(
                    f"No active group <code>{telegram_chat_id}</code>."
                )
                return
            await insert_audit_log(
                conn,
                action="bind_partner",
                actor_user_id=message.from_user.id if message.from_user else None,
                actor_internal_id=actor.id,
                target_entity="chat",
                target_id=updated.id,
                payload={
                    "telegram_chat_id": telegram_chat_id,
                    "partner_id": str(partner.id),
                    "partner_name": partner.name,
                },
            )
    log.info(
        "dm.bind_partner",
        chat_id=telegram_chat_id,
        partner=partner.name,
        by=str(actor.id),
    )
    await message.answer(
        f"🔗 Group <code>{telegram_chat_id}</code> bound to partner "
        f"<b>{html_escape(partner.name)}</b>."
    )


@router.message(Command("chat_delete"))
@require_role("admin")
async def cmd_chat_delete(
    message: Message,
    actor: InternalUser,
    command: CommandObject,
    bot: Bot,
    **kwargs: Any,
) -> None:
    """Disconnect a live unit and leave the chat when safe (admin only); audited.

    Usage: ``/chat_delete <id>`` where ``id`` is a unit id (full or first 8 chars)
    or a ``telegram_chat_id``. Sets ``status='removed'``. For a group/topic the bot
    leaves the Telegram chat only when no other live unit of it remains (leaving
    would kill sibling topics); a business unit is just removed (we can't leave a
    business DM). If the reference matches several units, you're asked to pick one
    by its unit id.
    """
    ref = (command.args or "").strip()
    if not ref:
        await message.answer(
            "Usage: <code>/chat_delete &lt;id&gt;</code> (unit id or telegram id)"
        )
        return
    ref_int = int(ref) if ref.lstrip("-").isdigit() else None

    async with acquire_connection() as conn:
        matches = await find_chats_by_ref(conn, ref, ref_int)
        live = [c for c in matches if c.status in ("active", "pending")]
        if not live:
            await message.answer("No active unit with that id.")
            return
        if len(live) > 1:
            lines = ["Multiple units match — re-run with a unit id (first 8 chars):"]
            for c in live[:_MAX_LIST_ROWS]:
                name = html_escape(c.chat_name) if c.chat_name else "(untitled)"
                lines.append(
                    f"• <code>{str(c.id)[:8]}</code> [{c.unit_type}] {name} [{c.status}]"
                )
            await message.answer("\n".join(lines))
            return

        chat = live[0]
        async with conn.transaction():
            removed = await deactivate_chat(
                conn, chat.telegram_chat_id, chat.message_thread_id
            )
            if removed is None:
                await message.answer("No active unit with that id.")
                return
            await insert_audit_log(
                conn,
                action="chat_delete",
                actor_user_id=message.from_user.id if message.from_user else None,
                actor_internal_id=actor.id,
                target_entity="chat",
                target_id=removed.id,
                payload={
                    "telegram_chat_id": chat.telegram_chat_id,
                    "thread_id": chat.message_thread_id,
                    "unit_type": chat.unit_type,
                },
            )
        # Decide whether leaving the Telegram chat is safe (after the unit is
        # already 'removed', so it's excluded from the live count).
        remaining = await count_live_units(conn, chat.telegram_chat_id)

    note = ""
    if chat.unit_type != "business":
        if remaining == 0:
            left = await _leave_chat_quietly(bot, chat.telegram_chat_id)
            note = " and left the chat" if left else " (couldn't leave — already gone?)"
        else:
            note = f" (staying — {remaining} other unit(s) still monitored)"
    log.info(
        "dm.chat_delete",
        unit_id=str(chat.id),
        chat_id=chat.telegram_chat_id,
        remaining=remaining,
        by=str(actor.id),
    )
    await message.answer(
        f"🗑 Unit <code>{str(chat.id)[:8]}</code> removed{note}."
    )


# --- Team / access management ------------------------------------------------
# The trusted-adder onboarding hinges on the adder being a whitelisted internal
# user, so an admin needs an in-bot way to grant/list/revoke that trust (no more
# hand-editing internal_users in Studio). A prospective manager learns their own
# Telegram id from the cover /whoami, passes it to an admin out-of-band, and the
# admin whitelists it here. All admin-only; all audited.


@router.message(Command("users"))
@require_role("admin")
async def cmd_users(
    message: Message, actor: InternalUser, command: CommandObject, **kwargs: Any
) -> None:
    """List internal users (admin only). ``/users all`` includes disabled ones."""
    include_disabled = (command.args or "").strip().lower() == "all"
    async with acquire_connection() as conn:
        users = await list_internal_users(conn, include_disabled=include_disabled)

    if not users:
        await message.answer("No internal users.")
        return
    scope = "all" if include_disabled else "enabled"
    lines = [f"<b>Internal users — {scope}</b> ({len(users)})"]
    for u in users[:_MAX_LIST_ROWS]:
        accounts = (
            ", ".join(f"<code>{a}</code>" for a in u.telegram_accounts)
            if u.telegram_accounts
            else "—"
        )
        flag = "" if u.enabled else " · <i>disabled</i>"
        lines.append(
            f"• <b>{html_escape(u.full_name)}</b> [{u.role}]{flag} — {accounts}"
        )
    if len(users) > _MAX_LIST_ROWS:
        lines.append(f"\n…and {len(users) - _MAX_LIST_ROWS} more (use /users all).")
    await message.answer("\n".join(lines))
    log.info("dm.users", actor=str(actor.id), count=len(users))


@router.message(Command("add_manager"))
@require_role("admin")
async def cmd_add_manager(
    message: Message, actor: InternalUser, command: CommandObject, **kwargs: Any
) -> None:
    """Whitelist a manager by Telegram id (admin only); audited.

    Usage: ``/add_manager <telegram_id> "Full Name"``. Once whitelisted, every chat
    the manager adds the bot to auto-activates (no per-chat approval). If the id
    already maps to an enabled user it's a no-op report; a disabled match is
    re-enabled instead of duplicated.
    """
    parsed = _parse_add_manager_args(command.args)
    if parsed is None:
        await message.answer(
            'Usage: <code>/add_manager &lt;telegram_id&gt; "Full Name"</code>\n'
            'Example: <code>/add_manager 123456789 "Ivan Petrov"</code>\n'
            "Tip: the person can get their id from /whoami and send it to you."
        )
        return
    telegram_id, full_name = parsed

    async with acquire_connection() as conn:
        async with conn.transaction():
            existing = await get_internal_user_by_telegram_id_any(conn, telegram_id)
            if existing is not None and existing.enabled:
                await message.answer(
                    f"Already whitelisted: <b>{html_escape(existing.full_name)}</b> "
                    f"[{existing.role}]."
                )
                return
            if existing is not None:  # disabled → restore trust
                await set_user_enabled(conn, existing.id, True)
                await insert_audit_log(
                    conn,
                    action="enable_user",
                    actor_user_id=message.from_user.id if message.from_user else None,
                    actor_internal_id=actor.id,
                    target_entity="internal_user",
                    target_id=existing.id,
                    payload={"telegram_id": telegram_id, "role": existing.role},
                )
                log.info("dm.enable_user", telegram_id=telegram_id, by=str(actor.id))
                await message.answer(
                    f"♻️ Re-enabled <b>{html_escape(existing.full_name)}</b> "
                    f"[{existing.role}]."
                )
                return
            created = await create_internal_user(
                conn, full_name=full_name, telegram_id=telegram_id, role="manager"
            )
            await insert_audit_log(
                conn,
                action="add_manager",
                actor_user_id=message.from_user.id if message.from_user else None,
                actor_internal_id=actor.id,
                target_entity="internal_user",
                target_id=created.id,
                payload={
                    "telegram_id": telegram_id,
                    "full_name": full_name,
                    "role": "manager",
                },
            )
    log.info("dm.add_manager", telegram_id=telegram_id, by=str(actor.id))
    await message.answer(
        f"✅ <b>{html_escape(full_name)}</b> whitelisted as <b>manager</b> "
        f"(id <code>{telegram_id}</code>).\n"
        "Chats they add me to now auto-activate — review them in /admin."
    )


@router.message(Command("disable_user"))
@require_role("admin")
async def cmd_disable_user(
    message: Message, actor: InternalUser, command: CommandObject, **kwargs: Any
) -> None:
    """Revoke an internal user's trust (admin only); audited.

    Usage: ``/disable_user <telegram_id | full name>``. Sets ``enabled=false`` — the
    user becomes an outsider: their commands stop and any chat they add henceforth
    goes to pending. Existing monitored chats are untouched. You can't disable
    yourself.
    """
    identifier = (command.args or "").strip()
    if not identifier:
        await message.answer(
            "Usage: <code>/disable_user &lt;telegram_id | full name&gt;</code>"
        )
        return

    async with acquire_connection() as conn:
        async with conn.transaction():
            target = await find_internal_user_by_identifier(conn, identifier)
            if target is None:
                await message.answer("User not found.")
                return
            if target.id == actor.id:
                await message.answer("You can't disable yourself.")
                return
            await set_user_enabled(conn, target.id, False)
            await insert_audit_log(
                conn,
                action="disable_user",
                actor_user_id=message.from_user.id if message.from_user else None,
                actor_internal_id=actor.id,
                target_entity="internal_user",
                target_id=target.id,
                payload={"role": target.role, "full_name": target.full_name},
            )
    log.info("dm.disable_user", target=str(target.id), by=str(actor.id))
    await message.answer(
        f"🚫 <b>{html_escape(target.full_name)}</b> [{target.role}] disabled. "
        "Existing chats stay; new adds from them will go to pending."
    )


# --- Risk review -------------------------------------------------------------


@router.message(Command("risks"))
@require_role("admin")
async def cmd_risks(
    message: Message, actor: InternalUser, command: CommandObject, **kwargs: Any
) -> None:
    """List recent risk events: default 20, or ``N``, or by partner name."""
    arg = (command.args or "").strip()
    owner_id = None if actor.role == "admin" else actor.id

    async with acquire_connection() as conn:
        if not arg:
            rows = await risk_q.list_recent(conn, limit=20, owner_id=owner_id)
            title = "recent"
        elif arg.isdigit():
            limit = min(int(arg), _MAX_RISK_LIMIT)
            rows = await risk_q.list_recent(conn, limit=limit, owner_id=owner_id)
            title = f"last {limit}"
        else:
            partner = await get_partner_by_name(conn, arg)
            if partner is None or (
                actor.role != "admin" and partner.owner_manager_id != actor.id
            ):
                await message.answer("Partner not found.")
                return
            rows = await risk_q.list_recent(
                conn, partner_id=partner.id, owner_id=owner_id, limit=20
            )
            title = html_escape(partner.name)

    if not rows:
        await message.answer(f"No risk events ({title}).")
        return
    lines = [f"<b>Risks — {title}</b> ({len(rows)})"]
    for r in rows:
        pname = html_escape(r.partner_name) if r.partner_name else "—"
        lines.append(
            f"<code>{str(r.id)[:8]}</code> [{r.risk_level}] {r.risk_type} | "
            f"{pname} | {_fmt_dt(r.created_at)}"
        )
    await message.answer("\n".join(lines))
    log.info("dm.risks", actor=str(actor.id), arg=arg, count=len(rows))


@router.message(Command("risk"))
@require_role("admin")
async def cmd_risk(
    message: Message, actor: InternalUser, command: CommandObject, **kwargs: Any
) -> None:
    """Show a risk event's detail card + the messages around the flagged one."""
    ref = (command.args or "").strip().lower()
    if not ref:
        await message.answer("Usage: <code>/risk &lt;id&gt;</code> (full or first 8 chars)")
        return

    async with acquire_connection() as conn:
        event = await risk_q.get_by_ref(conn, ref)
        if event is None or not await _risk_accessible(conn, actor, event.partner_id):
            await message.answer("Risk not found.")
            return
        anchor = (
            await get_message_by_id(conn, event.message_id)
            if event.message_id is not None
            else None
        )
        before: list[Any] = []
        after: list[Any] = []
        if event.chat_id is not None and anchor is not None:
            before, after = await get_messages_around(
                conn, event.chat_id, anchor.timestamp
            )

    lines = [
        f"<b>Risk {str(event.id)[:8]}</b> [{event.risk_level}] {event.risk_type}",
        f"Status: {event.status} · score {event.final_score}"
        + (
            f" · confidence {event.llm_confidence:.2f}"
            if event.llm_confidence is not None
            else ""
        ),
        f"Created: {_fmt_dt(event.created_at)}",
    ]
    if event.detected_phrase:
        lines.append(f"Phrase: “{html_escape(event.detected_phrase)}”")
    if event.llm_explanation:
        lines.append(f"\n{html_escape(event.llm_explanation)}")

    if anchor is not None:
        lines.append("\n<b>Context:</b>")
        for m in before:
            lines.append(_fmt_ctx_msg(m))
        lines.append("▶ " + _fmt_ctx_msg(anchor))
        for m in after:
            lines.append(_fmt_ctx_msg(m))
    await message.answer("\n".join(lines))
    log.info("dm.risk", actor=str(actor.id), risk_event_id=str(event.id))


@router.message(Command("mark_fp"))
@require_role("admin")
async def cmd_mark_fp(
    message: Message, actor: InternalUser, command: CommandObject, **kwargs: Any
) -> None:
    """Mark a risk event as a false positive."""
    await _mark_risk(message, actor, command, "false_positive")


@router.message(Command("mark_confirmed"))
@require_role("admin")
async def cmd_mark_confirmed(
    message: Message, actor: InternalUser, command: CommandObject, **kwargs: Any
) -> None:
    """Mark a risk event as confirmed."""
    await _mark_risk(message, actor, command, "confirmed")


@router.message(Command("mark_escalated"))
@require_role("admin")
async def cmd_mark_escalated(
    message: Message, actor: InternalUser, command: CommandObject, **kwargs: Any
) -> None:
    """Mark a risk event as escalated."""
    await _mark_risk(message, actor, command, "escalated")


# --- Settings (admin, read-only in MVP) --------------------------------------


@router.message(Command("thresholds"))
@require_role("admin")
async def cmd_thresholds(message: Message, actor: InternalUser, **kwargs: Any) -> None:
    """Show the current scoring / budget / cadence configuration (from config).

    The risk-level bands are read from ``settings`` (the single source of truth
    shared with ``src.pipeline.scoring``) so this display can never drift from the
    values the pipeline actually scores with.
    """
    med = settings.RISK_LEVEL_MEDIUM_MIN
    high = settings.RISK_LEVEL_HIGH_MIN
    crit = settings.RISK_LEVEL_CRITICAL_MIN
    await message.answer(
        "<b>Thresholds &amp; limits</b>\n"
        f"Priority lane threshold: <b>{settings.PRIORITY_SCORE_THRESHOLD}</b>\n"
        f"Daily LLM budget: <b>${settings.DAILY_LLM_BUDGET_USD}</b>\n\n"
        "<b>Risk levels (final score):</b>\n"
        f"Low: 0–{med - 1} (log only)\n"
        f"Medium: {med}–{high - 1} (summary)\n"
        f"High: {high}–{crit - 1} (real-time alert)\n"
        f"Critical: {crit}–100 (real-time alert)\n"
        f"Real-time alert fires at: <b>{settings.ALERT_MIN_RISK_LEVEL}+</b>\n\n"
        f"SLA reply threshold: {settings.SLA_RESPONSE_THRESHOLD_SECONDS // 60} min "
        f"(offline after {settings.SLA_OFFLINE_AFTER_SECONDS // 60})\n"
        f"Batch interval: {settings.BATCH_PROCESSING_INTERVAL_SECONDS // 60} min\n"
        f"Context window: {settings.CONTEXT_WINDOW_MINUTES} min\n"
        f"Pending chat timeout: {settings.ABANDONED_CHAT_TIMEOUT_HOURS // 24} days"
    )


@router.message(Command("categories"))
@require_role("admin")
async def cmd_categories(message: Message, actor: InternalUser, **kwargs: Any) -> None:
    """List the 12 fixed risk categories with a one-line gloss."""
    lines = ["<b>Risk categories (12)</b>"]
    for key, desc in _RISK_CATEGORIES:
        lines.append(f"• <b>{key}</b> — {desc}")
    await message.answer("\n".join(lines))


@router.message(Command("dictionary"))
@require_role("admin")
async def cmd_dictionary(
    message: Message, actor: InternalUser, command: CommandObject, **kwargs: Any
) -> None:
    """Show enabled Tier-1 patterns: top-20 by score, or all of one category."""
    category = (command.args or "").strip().lower() or None
    async with acquire_connection() as conn:
        patterns = await load_enabled_patterns(conn)

    if category is not None:
        selected = sorted(
            (p for p in patterns if p.risk_category == category),
            key=lambda p: p.base_score,
            reverse=True,
        )
        if not selected:
            await message.answer(f"No enabled patterns for category '{category}'.")
            return
        lines = [f"<b>Dictionary — {category}</b> ({len(selected)})"]
        lines += [
            f"• [{p.base_score}] {html_escape(p.pattern)} ({p.language})"
            for p in selected
        ]
    else:
        selected = sorted(patterns, key=lambda p: p.base_score, reverse=True)[:20]
        lines = [
            f"<b>Dictionary — top {len(selected)} by score</b> "
            f"({len(patterns)} enabled total)"
        ]
        lines += [
            f"• [{p.base_score}] {p.risk_category}: {html_escape(p.pattern)} "
            f"({p.language})"
            for p in selected
        ]
    await _send_lines(message, lines)


@router.message(Command("cost_status"))
@require_role("admin")
async def cmd_cost_status(message: Message, actor: InternalUser, **kwargs: Any) -> None:
    """Show today's LLM/Whisper spend vs budget + the 7-day total."""
    async with acquire_connection() as conn:
        today = await cost_get_today(conn)
        last7 = await sum_last_7_days(conn)

    budget = settings.DAILY_LLM_BUDGET_USD
    if today is None:
        await message.answer(
            "<b>Today</b>\nNo spend recorded yet.\n"
            f"Daily LLM budget: ${budget}\n"
            f"Last 7 days total: ${last7:.2f}"
        )
        return
    total = today.total_cost_usd if today.total_cost_usd is not None else (
        today.llm_cost_usd + today.whisper_cost_usd
    )
    cb = "TRIGGERED ⛔" if today.circuit_breaker_triggered else "ok"
    await message.answer(
        "<b>Today</b>\n"
        f"LLM: ${today.llm_cost_usd:.2f} ({today.llm_calls_count} calls)\n"
        f"Whisper: ${today.whisper_cost_usd:.2f} ({today.whisper_calls_count} calls)\n"
        f"Total: ${total:.2f} / ${budget}\n"
        f"Circuit breaker: {cb}\n\n"
        f"Last 7 days total: ${last7:.2f}"
    )


# --- Telegram Business connections (admin) -----------------------------------


@router.message(Command("business_connections"))
@require_role("admin")
async def cmd_business_connections(
    message: Message, actor: InternalUser, **kwargs: Any
) -> None:
    """List all Business connection grants with status and owner (admin only)."""
    async with acquire_connection() as conn:
        rows = await bc_q.list_all(conn)

    if not rows:
        await message.answer("No Business connection grants yet.")
        return

    lines = [f"<b>Business connections ({len(rows)})</b>"]
    for r in rows:
        owner = f"<code>{r.business_account_user_id}</code>"
        lines.append(
            f"• <code>{html_escape(r.business_connection_id)}</code> "
            f"[{r.status}] — owner {owner} — {_fmt_dt(r.connected_at)}"
        )
    await message.answer("\n".join(lines))


@router.message(Command("approve_business"))
@require_role("admin")
async def cmd_approve_business(
    message: Message, actor: InternalUser, command: CommandObject, **kwargs: Any
) -> None:
    """Approve a pending Business connection grant (admin only); audited."""
    bc_id = (command.args or "").strip()
    if not bc_id:
        await message.answer(
            "Usage: <code>/approve_business &lt;connection_id&gt;</code>"
        )
        return
    async with acquire_connection() as conn:
        async with conn.transaction():
            grant = await bc_q.get_by_connection_id(conn, bc_id)
            if grant is None:
                await message.answer("Business connection not found.")
                return
            if grant.status == "active":
                await message.answer("That connection is already active.")
                return
            await bc_q.update_status(
                conn, bc_id, "active", approved_by=actor.id, approved_at=datetime.now(UTC)
            )
            await insert_audit_log(
                conn,
                action="approve_business",
                actor_user_id=message.from_user.id if message.from_user else None,
                actor_internal_id=actor.id,
                target_entity="business_connection",
                target_id=grant.id,
                payload={"bc_id": bc_id, "old_status": grant.status},
            )
    log.info("dm.approve_business", bc_id=bc_id, by=str(actor.id))
    await message.answer(
        f"✅ Business connection <code>{html_escape(bc_id)}</code> approved — "
        "monitoring active."
    )


@router.message(Command("reject_business"))
@require_role("admin")
async def cmd_reject_business(
    message: Message, actor: InternalUser, command: CommandObject, **kwargs: Any
) -> None:
    """Decline a Business connection grant (admin only); audited.

    Sets ``status='disabled'`` (a deliberate refusal on our side, distinct from
    ``'revoked'`` which Telegram reports when the owner turns the bot off). Either
    way, business messages on that connection are dropped.
    """
    bc_id = (command.args or "").strip()
    if not bc_id:
        await message.answer(
            "Usage: <code>/reject_business &lt;connection_id&gt;</code>"
        )
        return
    async with acquire_connection() as conn:
        async with conn.transaction():
            grant = await bc_q.get_by_connection_id(conn, bc_id)
            if grant is None:
                await message.answer("Business connection not found.")
                return
            await bc_q.update_status(conn, bc_id, "disabled")
            await insert_audit_log(
                conn,
                action="reject_business",
                actor_user_id=message.from_user.id if message.from_user else None,
                actor_internal_id=actor.id,
                target_entity="business_connection",
                target_id=grant.id,
                payload={"bc_id": bc_id, "old_status": grant.status},
            )
    log.info("dm.reject_business", bc_id=bc_id, by=str(actor.id))
    await message.answer(
        f"🚫 Business connection <code>{html_escape(bc_id)}</code> disabled."
    )


@router.message(Command("link_business_chat"))
@require_role("admin")
async def cmd_link_business_chat(
    message: Message, actor: InternalUser, command: CommandObject, **kwargs: Any
) -> None:
    """Link an unknown business DM to a partner and activate it (admin only)."""
    parsed = _parse_link_business_args(command.args)
    if parsed is None:
        await message.answer(
            "Usage: <code>/link_business_chat &lt;connection_id&gt; "
            '&lt;peer_user_id&gt; "Partner Name"</code>'
        )
        return
    bc_id, peer_user_id, partner_name = parsed

    async with acquire_connection() as conn:
        async with conn.transaction():
            grant = await bc_q.get_by_connection_id(conn, bc_id)
            if grant is None:
                await message.answer("Business connection not found.")
                return
            partner = await get_or_create_partner(conn, partner_name)
            unit = await link_business_chat(
                conn,
                telegram_chat_id=peer_user_id,
                business_connection_id=bc_id,
                business_peer_user_id=peer_user_id,
                partner_id=partner.id,
                authorized_by=actor.id,
            )
            if unit is None:
                await message.answer(
                    "Couldn't link — that id is held by a non-business unit."
                )
                return
            await insert_audit_log(
                conn,
                action="link_business_chat",
                actor_user_id=message.from_user.id if message.from_user else None,
                actor_internal_id=actor.id,
                target_entity="chat",
                target_id=unit.id,
                payload={
                    "bc_id": bc_id,
                    "peer_user_id": peer_user_id,
                    "partner_id": str(partner.id),
                },
            )
    log.info(
        "dm.link_business_chat",
        bc_id=bc_id,
        peer_user_id=peer_user_id,
        partner_id=str(partner.id),
        by=str(actor.id),
    )
    await message.answer(
        f"🔗 Business chat with <code>{peer_user_id}</code> linked to "
        f"<b>{html_escape(partner.name)}</b> — monitoring active."
    )


async def _mark_risk(
    message: Message, actor: InternalUser, command: CommandObject, new_status: str
) -> None:
    """Shared body for /mark_fp · /mark_confirmed · /mark_escalated.

    Managers may only review risks of partners they own; the check happens inside
    the transaction, before the UPDATE. The status change + audit row commit
    together.
    """
    ref = (command.args or "").strip().lower()
    if not ref:
        await message.answer("Usage: <code>/mark_… &lt;id&gt;</code> (full or first 8 chars)")
        return

    async with acquire_connection() as conn:
        async with conn.transaction():
            event = await risk_q.get_by_ref(conn, ref)
            if event is None or not await _risk_accessible(conn, actor, event.partner_id):
                await message.answer("Risk not found.")
                return
            old = event.status
            await risk_q.update_status(conn, event.id, new_status, actor.id)
            await insert_audit_log(
                conn,
                action=f"mark_{new_status}",
                actor_user_id=message.from_user.id if message.from_user else None,
                actor_internal_id=actor.id,
                target_entity="risk_event",
                target_id=event.id,
                payload={
                    "risk_event_id": str(event.id),
                    "old_status": old,
                    "new_status": new_status,
                },
            )
    log.info(
        "dm.mark_risk",
        risk_event_id=str(event.id),
        old=old,
        new=new_status,
        by=str(actor.id),
    )
    await message.answer(f"Risk {str(event.id)[:8]}: {old} → {new_status}")


async def _risk_accessible(
    conn: Any, actor: InternalUser, partner_id: Any
) -> bool:
    """True if the actor may view/act on a risk of ``partner_id``.

    Admins always may; a manager may only when they own that partner. A risk with
    no partner is admin-only.
    """
    if actor.role == "admin":
        return True
    if partner_id is None:
        return False
    partner = await get_partner_by_id(conn, partner_id)
    return partner is not None and partner.owner_manager_id == actor.id


def _fmt_dt(value: datetime | None) -> str:
    """Format a timestamp for DM output (UTC, minute precision), or em dash."""
    return value.strftime("%Y-%m-%d %H:%M") if value is not None else "—"


def _fmt_ctx_msg(msg: Any) -> str:
    """One-line context message: time · role · truncated text/transcription."""
    text = msg.message_text or msg.transcription or f"[{msg.message_type}]"
    snippet = html_escape(text[:80])
    return f"<code>{_fmt_dt(msg.timestamp)}</code> {msg.sender_role}: {snippet}"


async def _send_lines(message: Message, lines: list[str], *, max_chars: int = 3500) -> None:
    """Send lines in as few messages as fit under Telegram's size limit."""
    buf: list[str] = []
    size = 0
    for line in lines:
        if buf and size + len(line) + 1 > max_chars:
            await message.answer("\n".join(buf))
            buf, size = [], 0
        buf.append(line)
        size += len(line) + 1
    if buf:
        await message.answer("\n".join(buf))


def _split_name_last(args: str | None) -> tuple[str, str] | None:
    """Split ``<name…> <last_token>`` on the final whitespace.

    The name may contain spaces (optionally quoted); the trailing token is a
    status / identifier. Returns ``(name, last_token)`` or ``None`` if malformed.
    """
    if not args:
        return None
    parts = args.strip().rsplit(maxsplit=1)
    if len(parts) != 2:
        return None
    name = _strip_quotes(parts[0])
    last = parts[1].strip()
    if not name or not last:
        return None
    return name, last


async def _leave_chat_quietly(bot: Bot, telegram_chat_id: int) -> bool:
    """Leave a chat, swallowing API errors (it may already be gone). Returns success."""
    from aiogram.exceptions import TelegramAPIError

    try:
        await bot.leave_chat(telegram_chat_id)
        return True
    except TelegramAPIError as exc:
        log.warning("onboarding.leave_failed", chat_id=telegram_chat_id, error=str(exc))
        return False


def _strip_quotes(value: str) -> str:
    """Drop a single pair of surrounding quotes from a partner name, if present."""
    value = value.strip()
    if len(value) >= 2 and value[0] == value[-1] and value[0] in {'"', "'"}:
        return value[1:-1].strip()
    return value


def _parse_authorize_args(args: str | None) -> tuple[int, str] | None:
    """Split ``<chat_id> <partner name>`` or return ``None`` (group authorize)."""
    if not args:
        return None
    parts = args.strip().split(maxsplit=1)
    if len(parts) != 2:
        return None
    chat_id = _parse_int(parts[0])
    partner_name = _strip_quotes(parts[1])
    if chat_id is None or not partner_name:
        return None
    return chat_id, partner_name


def _parse_reject_args(args: str | None) -> int | None:
    """Parse the single ``<chat_id>`` token (group reject), or ``None``."""
    if not args:
        return None
    parts = args.strip().split()
    if len(parts) != 1:
        return None
    return _parse_int(parts[0])


def _parse_authorize_topic_args(args: str | None) -> tuple[int, int, str] | None:
    """Split ``<chat_id> <thread_id> <partner name>`` or return ``None``."""
    if not args:
        return None
    parts = args.strip().split(maxsplit=2)
    if len(parts) != 3:
        return None
    chat_id = _parse_int(parts[0])
    thread_id = _parse_int(parts[1])
    partner_name = _strip_quotes(parts[2])
    if chat_id is None or thread_id is None or not partner_name:
        return None
    return chat_id, thread_id, partner_name


def _parse_reject_topic_args(args: str | None) -> tuple[int, int] | None:
    """Split ``<chat_id> <thread_id>`` into ``(chat_id, thread_id)`` or ``None``."""
    if not args:
        return None
    parts = args.strip().split()
    if len(parts) != 2:
        return None
    chat_id = _parse_int(parts[0])
    thread_id = _parse_int(parts[1])
    if chat_id is None or thread_id is None:
        return None
    return chat_id, thread_id


def _parse_add_manager_args(args: str | None) -> tuple[int, str] | None:
    """Split ``<telegram_id> <full name>`` for /add_manager, or return ``None``.

    The id must be a positive integer (Telegram user ids are positive — a negative
    value would be a chat id, a likely paste error). The name may be quoted and
    contain spaces.
    """
    if not args:
        return None
    parts = args.strip().split(maxsplit=1)
    if len(parts) != 2:
        return None
    telegram_id = _parse_int(parts[0])
    full_name = _strip_quotes(parts[1])
    if telegram_id is None or telegram_id <= 0 or not full_name:
        return None
    return telegram_id, full_name


def _parse_link_business_args(args: str | None) -> tuple[str, int, str] | None:
    """Split ``<connection_id> <peer_user_id> <partner name>`` for /link_business_chat.

    Returns ``(connection_id, peer_user_id, partner_name)`` or ``None`` if
    malformed. The partner name may be quoted and contain spaces.
    """
    if not args:
        return None
    parts = args.strip().split(maxsplit=2)
    if len(parts) != 3:
        return None
    bc_id = parts[0].strip()
    peer_user_id = _parse_int(parts[1])
    partner_name = _strip_quotes(parts[2])
    if not bc_id or peer_user_id is None or not partner_name:
        return None
    return bc_id, peer_user_id, partner_name


def _parse_int(token: str | None) -> int | None:
    """Parse a single integer token (chat id is negative for groups), or ``None``."""
    if not token:
        return None
    try:
        return int(token.strip())
    except ValueError:
        return None
