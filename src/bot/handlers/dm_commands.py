"""/start, /authorize, /pending, /partners, etc. Phase 3/4/13.

Phase 3 ships the read-only identity commands: ``/start``, ``/help``, ``/whoami``.
Phase 4 adds the onboarding-control commands ``/authorize``, ``/reject``,
``/pending``; the partner/risk commands land in Phase 13.

Every handler here is restricted to **private** chats by a router-level filter,
which structurally enforces CLAUDE.md's hard rule: the bot never writes in a
partner group chat, only in DMs with our own staff. The authorization commands
additionally require the caller to be an enabled internal user.
"""

from __future__ import annotations

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
    get_chat_by_telegram_id,
    list_pending_chats,
    reject_chat,
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
    "<b>Internal only — chat onboarding:</b>\n"
    "/pending — chats awaiting authorization\n"
    "/authorize &lt;chat_id&gt; &lt;partner name&gt; — start monitoring a chat\n"
    "/reject &lt;chat_id&gt; — decline a chat and leave it\n\n"
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

    role = internal.role or "—"
    admin = "yes" if internal.is_admin else "no"
    await message.answer(
        "You are recognized as an internal user.\n\n"
        f"<b>Name:</b> {internal.full_name}\n"
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


@router.message(Command("pending"))
async def cmd_pending(message: Message) -> None:
    """List chats awaiting authorization (internal only)."""
    async with acquire_connection() as conn:
        if await _resolve_internal(message, conn) is None:
            return
        pending = await list_pending_chats(conn)

    if not pending:
        await message.answer("No chats are pending authorization. ✅")
        return

    lines = ["<b>Chats pending authorization</b>\n"]
    for chat in pending:
        name = chat.chat_name or "(untitled)"
        lines.append(
            f"• {name}\n"
            f"  id: <code>{chat.telegram_chat_id}</code>"
            f" · added by <code>{chat.added_by_user_id or '—'}</code>"
        )
    lines.append(
        "\nAuthorize with <code>/authorize &lt;chat_id&gt; &lt;partner name&gt;</code> "
        "or <code>/reject &lt;chat_id&gt;</code>."
    )
    await message.answer("\n".join(lines))


@router.message(Command("authorize"))
async def cmd_authorize(message: Message, command: CommandObject) -> None:
    """Activate a pending chat and bind it to a partner (internal only).

    Usage: ``/authorize <chat_id> <partner name>``. The partner is created if it
    does not exist. The activation + partner upsert + audit row commit together.
    """
    parsed = _parse_authorize_args(command.args)
    if parsed is None:
        await message.answer(
            "Usage: <code>/authorize &lt;chat_id&gt; &lt;partner name&gt;</code>\n"
            "Example: <code>/authorize -1001234567890 Acme Corp</code>"
        )
        return
    telegram_chat_id, partner_name = parsed

    async with acquire_connection() as conn:
        internal = await _resolve_internal(message, conn)
        if internal is None:
            return

        async with conn.transaction():
            chat = await get_chat_by_telegram_id(conn, telegram_chat_id)
            if chat is None:
                await message.answer(
                    f"I don't know chat <code>{telegram_chat_id}</code>. "
                    "I only track chats I've been added to."
                )
                return
            if chat.status != "pending":
                await message.answer(
                    f"Chat <code>{telegram_chat_id}</code> is not pending "
                    f"(current status: <b>{chat.status}</b>). Nothing to do."
                )
                return

            partner = await get_or_create_partner(conn, partner_name)
            activated = await authorize_chat(
                conn,
                telegram_chat_id=telegram_chat_id,
                partner_id=partner.id,
                authorized_by=internal.id,
            )
            if activated is None:  # lost a race; chat left pending state
                await message.answer(
                    f"Chat <code>{telegram_chat_id}</code> is no longer pending. "
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
        f"✅ Chat activated and bound to partner <b>{partner.name}</b>. "
        "Monitoring has started."
    )


@router.message(Command("reject"))
async def cmd_reject(message: Message, command: CommandObject, bot: Bot) -> None:
    """Decline a pending chat: mark it banned and leave it (internal only).

    Usage: ``/reject <chat_id>``. The DB flip + audit commit together; the
    ``bot.leave_chat`` call happens after commit and is best-effort.
    """
    telegram_chat_id = _parse_chat_id(command.args)
    if telegram_chat_id is None:
        await message.answer(
            "Usage: <code>/reject &lt;chat_id&gt;</code>\n"
            "Example: <code>/reject -1001234567890</code>"
        )
        return

    async with acquire_connection() as conn:
        internal = await _resolve_internal(message, conn)
        if internal is None:
            return

        async with conn.transaction():
            rejected = await reject_chat(conn, telegram_chat_id)
            if rejected is None:
                await message.answer(
                    f"Chat <code>{telegram_chat_id}</code> is not pending. "
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

    left = await _leave_chat_quietly(bot, telegram_chat_id)
    log.info("onboarding.rejected", chat_id=telegram_chat_id, left=left, by=internal.full_name)
    suffix = "" if left else " (I couldn't leave it — already removed?)"
    await message.answer(
        f"🚫 Chat <code>{telegram_chat_id}</code> rejected and banned.{suffix}"
    )


async def _leave_chat_quietly(bot: Bot, telegram_chat_id: int) -> bool:
    """Leave a chat, swallowing API errors (it may already be gone). Returns success."""
    from aiogram.exceptions import TelegramAPIError

    try:
        await bot.leave_chat(telegram_chat_id)
        return True
    except TelegramAPIError as exc:
        log.warning("onboarding.leave_failed", chat_id=telegram_chat_id, error=str(exc))
        return False


def _parse_authorize_args(args: str | None) -> tuple[int, str] | None:
    """Split ``<chat_id> <partner name>`` into ``(chat_id, name)`` or ``None``."""
    if not args:
        return None
    parts = args.strip().split(maxsplit=1)
    if len(parts) != 2:
        return None
    chat_id = _parse_chat_id(parts[0])
    partner_name = parts[1].strip()
    if chat_id is None or not partner_name:
        return None
    return chat_id, partner_name


def _parse_chat_id(token: str | None) -> int | None:
    """Parse a Telegram chat id (negative for groups) from a string, or ``None``."""
    if not token:
        return None
    try:
        return int(token.strip().split(maxsplit=1)[0])
    except ValueError:
        return None
