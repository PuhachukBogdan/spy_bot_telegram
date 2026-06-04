"""/start, /authorize, /pending, /partners, etc. Phase 3/4/13.

Phase 3 ships the read-only identity commands: ``/start``, ``/help``, ``/whoami``.
Phase 4 adds the onboarding-control commands; the partner/risk commands land in
Phase 13.

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
partner group chat, only in DMs with our own staff. Group onboarding requires an
enabled internal user; topic onboarding additionally requires admin.

Manual e2e test scenario (forum topics)::

    1. Create a Telegram supergroup with "Topics" enabled.
    2. Add the bot -> a pending GROUP unit is created; admins get a DM.
    3. /authorize <chat_id> "TestPartner"   -> group becomes active.
    4. Create a topic "Ops" and post a message in it.
    5. The first message is dropped, a pending TOPIC unit is created, and admins
       get a "📂 New topic pending" DM (only because the parent group is active).
    6. /authorize_topic <chat_id> <thread_id> "TestPartner Ops" -> topic active.
    7. Further messages in "Ops" are ingested under the topic unit's own chats.id
       (UUID), separate from the group-level unit.
    8. /reject_topic <chat_id> <other_thread_id> -> that topic is 'rejected' and
       the bot stays in the supergroup for "Ops".
"""

from __future__ import annotations

from html import escape as html_escape

import asyncpg
from aiogram import Bot, F, Router
from aiogram.enums import ChatType
from aiogram.filters import Command, CommandObject, CommandStart
from aiogram.types import Message

from src.db.client import acquire_connection
from src.db.models import InternalUser
from src.db.queries.audit import insert_audit_log
from src.db.queries.chats import (
    authorize_chat,
    count_live_units,
    get_chat_unit,
    list_pending,
    reject_chat,
    reject_topic,
)
from src.db.queries.etc import find_internal_user_by_telegram_id
from src.db.queries.partners import get_or_create_partner
from src.utils.logging import get_logger

log = get_logger(__name__)

router = Router(name="dm_commands")
# DM-only: this filter applies to every message handler on this router, so none
# of these commands can ever fire inside a partner group.
router.message.filter(F.chat.type == ChatType.PRIVATE)


_START_TEXT = (
    "<b>Partner Chat Risk Monitor</b>\n\n"
    "I silently monitor partner group chats for risk signals and report to "
    "management. I never post in partner chats — I only talk here, in DMs with "
    "internal staff.\n\n"
    "By messaging me you've enabled DMs, so I can now reach you with the "
    "notifications your role allows.\n\n"
    "Use /help to see what I can do, and /whoami to check how I recognize you."
)

_HELP_TEXT = (
    "<b>Available commands</b>\n\n"
    "/start — intro and what I do\n"
    "/help — this message\n"
    "/whoami — show whether I recognize you as an internal user\n\n"
    "<b>Internal only — onboarding:</b>\n"
    "/pending — units (groups + topics) awaiting authorization\n"
    "/authorize &lt;chat_id&gt; &lt;partner name&gt; — activate a group\n"
    "/reject &lt;chat_id&gt; — decline a group (bot leaves it)\n\n"
    "<b>Admin only — forum topics:</b>\n"
    "/authorize_topic &lt;chat_id&gt; &lt;thread_id&gt; &lt;partner name&gt; — activate a topic\n"
    "/reject_topic &lt;chat_id&gt; &lt;thread_id&gt; — decline a topic (bot stays in the chat)\n\n"
    "<i>Partner and risk views unlock in later phases.</i>"
)


@router.message(CommandStart())
async def cmd_start(message: Message) -> None:
    """Greet the user and explain the bot's purpose."""
    await message.answer(_START_TEXT)


@router.message(Command("help"))
async def cmd_help(message: Message) -> None:
    """List the currently available commands."""
    await message.answer(_HELP_TEXT)


@router.message(Command("whoami"))
async def cmd_whoami(message: Message) -> None:
    """Report how the bot identifies the caller (internal user or outsider)."""
    user = message.from_user
    if user is None:  # defensive: private messages always carry from_user
        await message.answer("I can't read your account details.")
        return

    async with acquire_connection() as conn:
        internal = await find_internal_user_by_telegram_id(conn, user.id)

    if internal is None:
        await message.answer(
            "You are <b>not</b> recognized as an internal user.\n"
            f"Your Telegram id: <code>{user.id}</code>\n\n"
            "If you should have access, ask an admin to add this id to your "
            "<code>internal_users</code> record."
        )
        return

    role = html_escape(internal.role) if internal.role else "—"
    admin = "yes" if internal.is_admin else "no"
    await message.answer(
        "You are recognized as an internal user.\n\n"
        f"<b>Name:</b> {html_escape(internal.full_name)}\n"
        f"<b>Role:</b> {role}\n"
        f"<b>Admin:</b> {admin}\n"
        f"<b>Telegram id:</b> <code>{user.id}</code>"
    )


async def _resolve_internal(
    message: Message, conn: asyncpg.Connection
) -> InternalUser | None:
    """Return the calling internal user, or ``None`` after replying to outsiders.

    Gate for the onboarding-control commands: a Telegram account not mapped to an
    enabled ``internal_users`` row is told it lacks access and the command is not
    executed.
    """
    user = message.from_user
    if user is None:  # private messages always carry from_user; defensive
        await message.answer("I can't read your account details.")
        return None
    internal = await find_internal_user_by_telegram_id(conn, user.id)
    if internal is None:
        await message.answer(
            "You're not authorized to use this command. It's restricted to "
            "internal staff."
        )
        log.info("dm.command_denied", user_id=user.id)
    return internal


async def _resolve_admin(
    message: Message, conn: asyncpg.Connection
) -> InternalUser | None:
    """Return the caller only if they're an enabled internal **admin**.

    Topic onboarding is admin-gated (group onboarding needs only internal staff):
    topic discovery DMs go to admins, so authorizing/rejecting them is too.
    """
    internal = await _resolve_internal(message, conn)
    if internal is None:
        return None
    if not internal.is_admin:
        await message.answer("This command is restricted to admins.")
        log.info("dm.command_denied_not_admin", user_id=internal.id)
        return None
    return internal


@router.message(Command("pending"))
async def cmd_pending(message: Message) -> None:
    """List units awaiting authorization, split into groups and topics."""
    async with acquire_connection() as conn:
        if await _resolve_internal(message, conn) is None:
            return
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
async def cmd_authorize(message: Message, command: CommandObject) -> None:
    """Activate a pending GROUP and bind it to a partner (internal only).

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
        internal = await _resolve_internal(message, conn)
        if internal is None:
            return

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
                authorized_by=internal.id,
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
                actor_internal_id=internal.id,
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
        by=internal.full_name,
    )
    await message.answer(
        f"✅ Group activated and bound to partner <b>{html_escape(partner.name)}</b>. "
        "Monitoring has started."
    )


@router.message(Command("reject"))
async def cmd_reject(message: Message, command: CommandObject, bot: Bot) -> None:
    """Decline a pending GROUP: mark it banned and leave it (internal only).

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
        internal = await _resolve_internal(message, conn)
        if internal is None:
            return

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
                actor_internal_id=internal.id,
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
        by=internal.full_name,
    )
    await message.answer(
        f"🚫 Group <code>{telegram_chat_id}</code> rejected and banned {body}."
    )


@router.message(Command("authorize_topic"))
async def cmd_authorize_topic(message: Message, command: CommandObject) -> None:
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
        internal = await _resolve_admin(message, conn)
        if internal is None:
            return

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
                authorized_by=internal.id,
            )
            if activated is None:  # lost a race
                await message.answer("No pending topic with that id.")
                return

            await insert_audit_log(
                conn,
                action="authorize_topic",
                actor_user_id=message.from_user.id if message.from_user else None,
                actor_internal_id=internal.id,
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
        by=internal.full_name,
    )
    await message.answer(
        f"✅ Topic activated, monitoring started for "
        f"<b>{html_escape(partner.name)}</b>."
    )


@router.message(Command("reject_topic"))
async def cmd_reject_topic(message: Message, command: CommandObject) -> None:
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
        internal = await _resolve_admin(message, conn)
        if internal is None:
            return

        async with conn.transaction():
            rejected = await reject_topic(conn, telegram_chat_id, thread_id)
            if rejected is None:
                await message.answer("No pending topic with that id.")
                return
            await insert_audit_log(
                conn,
                action="reject_topic",
                actor_user_id=message.from_user.id if message.from_user else None,
                actor_internal_id=internal.id,
                target_entity="chat",
                target_id=rejected.id,
                payload={"telegram_chat_id": telegram_chat_id, "thread_id": thread_id},
            )

    log.info(
        "onboarding.topic_rejected",
        chat_id=telegram_chat_id,
        thread_id=thread_id,
        by=internal.full_name,
    )
    # Deliberately no leave_chat: the bot remains for the other topics.
    await message.answer("🚫 Topic rejected. The bot stays in the chat for other topics.")


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


def _parse_int(token: str | None) -> int | None:
    """Parse a single integer token (chat id is negative for groups), or ``None``."""
    if not token:
        return None
    try:
        return int(token.strip())
    except ValueError:
        return None
