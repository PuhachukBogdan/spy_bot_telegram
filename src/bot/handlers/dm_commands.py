"""/start, /authorize, /pending, /partners, etc. Phase 3/4/13.

Phase 3 ships the read-only identity commands: ``/start``, ``/help``, ``/whoami``.
Authorization (`/authorize`, `/reject`, `/pending`) lands in Phase 4 and the
partner/risk commands in Phase 13.

Every handler here is restricted to **private** chats by a router-level filter,
which structurally enforces CLAUDE.md's hard rule: the bot never writes in a
partner group chat, only in DMs with our own staff.
"""

from __future__ import annotations

from aiogram import F, Router
from aiogram.enums import ChatType
from aiogram.filters import Command, CommandStart
from aiogram.types import Message

from src.db.client import acquire_connection
from src.db.queries.etc import find_internal_user_by_telegram_id
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
    "<i>More commands (chat authorization, partner and risk views) unlock in "
    "later phases.</i>"
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
