"""Uniform reply to an unrecognized command in a DM — a cover, not a courtesy.

Every role-gated command answers ``"Command not found."`` to a caller who may
not use it (see :func:`src.bot.middleware.roles.require_role`), the point being
that a manager who types ``/dashboard`` cannot tell it apart from a command that
does not exist.

That only holds if a command which genuinely does not exist answers the same
way. It did not: an unknown ``/...`` in a private chat matched nothing in the
command routers, fell through to the group ingestion catch-all, found no chat
row for the DM and returned in silence. So ``/dashboard`` produced a reply and
``/asdfgh`` produced nothing — and the difference is the disclosure the neutral
wording exists to prevent.

This router closes that gap: any unmatched command in a private chat gets the
identical line. It is included last among the private-chat routers, so a real
command is always matched by its own handler first.
"""

from __future__ import annotations

from aiogram import F, Router
from aiogram.enums import ChatType
from aiogram.types import Message

router = Router(name="unknown_commands")
router.message.filter(F.chat.type == ChatType.PRIVATE)


@router.message(F.text.startswith("/"))
async def unknown_command(message: Message) -> None:
    """Answer an unmatched private command exactly as a forbidden one is answered."""
    await message.answer("Command not found.")
