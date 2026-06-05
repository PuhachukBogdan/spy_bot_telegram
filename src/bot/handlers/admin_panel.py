"""Inline admin oversight panel — ``/admin``. Phase 13 (trusted-adder model).

A single command, ``/admin``, opens an inline-button panel that answers the
question the trusted-adder onboarding raises: *who connected the bot, and where?*
Per-chat approvals are gone (a known internal user's chats auto-activate), so the
admin's job shifts from gatekeeping to oversight — this panel is that surface.

Navigation (all read-only; the destructive action is the typed ``/chat_delete``):

  * home          → people who connected ≥1 live unit (+ unattributed + business)
  * ap:u:<uuid>   → that person's live units, each with copyable ids + delete hint
  * ap:unattr     → live units whose adder is not a known internal user
  * ap:bc         → Business connection grants

The panel is ADMIN-ONLY. The ``/admin`` message handler is gated by
``require_role('admin')``; the callback handlers re-resolve the presser and stay
silent for anyone who is not an admin (callbacks bypass message middleware, and a
non-admin must never learn the panel exists — cover posture). Like every handler
in the DM surface, this router is private-chat-only.
"""

from __future__ import annotations

from html import escape as html_escape
from typing import Any
from uuid import UUID

from aiogram import F, Router
from aiogram.enums import ChatType
from aiogram.exceptions import TelegramBadRequest
from aiogram.filters import Command
from aiogram.types import (
    CallbackQuery,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    Message,
)

from src.bot.middleware.roles import require_role
from src.db.client import acquire_connection
from src.db.models import InternalUser
from src.db.queries import business_connections as bc_q
from src.db.queries.chats import (
    count_unattributed_chats,
    list_chats_by_adder,
    list_managers_with_chat_counts,
    list_unattributed_chats,
)
from src.db.queries.etc import (
    find_internal_user_by_telegram_id,
    get_internal_user_by_id,
)
from src.utils.logging import get_logger

log = get_logger(__name__)

router = Router(name="admin_panel")
# DM-only, mirroring dm_commands: /admin can never fire inside a partner group.
router.message.filter(F.chat.type == ChatType.PRIVATE)

_MAX_PANEL_ROWS = 25


def _back_kb() -> InlineKeyboardMarkup:
    """A single ``← Back`` button returning to the panel home."""
    return InlineKeyboardMarkup(
        inline_keyboard=[[InlineKeyboardButton(text="← Back", callback_data="ap:home")]]
    )


async def _render_home(conn: Any) -> tuple[str, InlineKeyboardMarkup]:
    """Panel home: one button per connector, plus unattributed + business."""
    managers = await list_managers_with_chat_counts(conn)
    unattributed = await count_unattributed_chats(conn)

    rows: list[list[InlineKeyboardButton]] = []
    for m in managers[:_MAX_PANEL_ROWS]:
        rows.append(
            [
                InlineKeyboardButton(
                    text=f"{m.full_name} [{m.role}] — {m.chat_count}",
                    callback_data=f"ap:u:{m.internal_user_id}",
                )
            ]
        )
    if unattributed > 0:
        rows.append(
            [
                InlineKeyboardButton(
                    text=f"❓ Unattributed — {unattributed}",
                    callback_data="ap:unattr",
                )
            ]
        )
    rows.append(
        [InlineKeyboardButton(text="🔗 Business connections", callback_data="ap:bc")]
    )

    text = (
        "<b>📊 Admin panel — connected chats</b>\n\n"
        "Who connected the bot, and where. Tap a person to see their units."
    )
    if not managers and unattributed == 0:
        text += "\n\n<i>No active chats yet.</i>"
    return text, InlineKeyboardMarkup(inline_keyboard=rows)


def _format_units(lines: list[str], chats: list[Any]) -> None:
    """Append a formatted block per unit (name, type/status, ids, delete hint)."""
    if not chats:
        lines.append("<i>No active units.</i>")
        return
    for c in chats[:_MAX_PANEL_ROWS]:
        name = html_escape(c.chat_name) if c.chat_name else "(untitled)"
        partner = html_escape(c.partner_name) if c.partner_name else "—"
        lines.append(
            f"• <b>{name}</b> [{c.unit_type}] {c.status}\n"
            f"  chat id: <code>{c.telegram_chat_id}</code> · partner: {partner}\n"
            f"  remove: <code>/chat_delete {str(c.id)[:8]}</code>"
        )
    if len(chats) > _MAX_PANEL_ROWS:
        lines.append(f"\n…and {len(chats) - _MAX_PANEL_ROWS} more.")


async def _render_user(
    conn: Any, internal_user_id: UUID
) -> tuple[str, InlineKeyboardMarkup]:
    """Drill-down: the live units one internal user connected."""
    user = await get_internal_user_by_id(conn, internal_user_id)
    if user is None:
        return "User not found.", _back_kb()
    chats = await list_chats_by_adder(conn, internal_user_id)
    lines = [
        f"👤 <b>{html_escape(user.full_name)}</b> [{user.role}] — {len(chats)} unit(s)\n"
    ]
    _format_units(lines, chats)
    return "\n".join(lines), _back_kb()


async def _render_unattributed(conn: Any) -> tuple[str, InlineKeyboardMarkup]:
    """Drill-down: live units whose adder is not a known internal user."""
    chats = await list_unattributed_chats(conn)
    lines = [f"❓ <b>Unattributed units</b> — {len(chats)}\n"]
    _format_units(lines, chats)
    return "\n".join(lines), _back_kb()


async def _render_bc(conn: Any) -> tuple[str, InlineKeyboardMarkup]:
    """Drill-down: Business connection grants (status + owner)."""
    rows = await bc_q.list_all(conn)
    lines = [f"🔗 <b>Business connections</b> — {len(rows)}\n"]
    if not rows:
        lines.append("<i>None yet.</i>")
    for r in rows[:_MAX_PANEL_ROWS]:
        lines.append(
            f"• <code>{html_escape(r.business_connection_id)}</code> [{r.status}]\n"
            f"  owner: <code>{r.business_account_user_id}</code>"
        )
    return "\n".join(lines), _back_kb()


@router.message(Command("admin"))
@require_role("admin")
async def cmd_admin(message: Message, actor: InternalUser, **kwargs: Any) -> None:
    """Open the inline oversight panel (admin only)."""
    async with acquire_connection() as conn:
        text, kb = await _render_home(conn)
    await message.answer(text, reply_markup=kb)
    log.info("dm.admin_panel", actor=str(actor.id))


async def _gate_admin(callback: CallbackQuery) -> bool:
    """Return True iff the presser is an enabled admin; else silently dismiss.

    Callbacks bypass the message middleware/decorators, so we re-resolve here. A
    non-admin (or anyone who somehow forwarded the panel) gets a no-op ack and
    learns nothing — same cover posture as the command surface.
    """
    user = callback.from_user
    if user is None:
        await callback.answer()
        return False
    async with acquire_connection() as conn:
        actor = await find_internal_user_by_telegram_id(conn, user.id)
    if actor is None or actor.role != "admin":
        await callback.answer()
        log.info("rbac.panel_denied", user_id=user.id)
        return False
    return True


async def _edit_panel(
    callback: CallbackQuery, text: str, kb: InlineKeyboardMarkup
) -> None:
    """Edit the panel message in place; swallow the 'not modified' no-op error."""
    message = callback.message
    # An old/inaccessible message is None or an InaccessibleMessage (no edit_text).
    if not isinstance(message, Message):
        await callback.answer()
        return
    try:
        await message.edit_text(text, reply_markup=kb)
    except TelegramBadRequest as exc:
        # Re-tapping the same button yields "message is not modified" — benign.
        if "not modified" not in str(exc).lower():
            raise
    await callback.answer()


@router.callback_query(F.data == "ap:home")
async def cb_home(callback: CallbackQuery) -> None:
    """Re-render the panel home (the ← Back target)."""
    if not await _gate_admin(callback):
        return
    async with acquire_connection() as conn:
        text, kb = await _render_home(conn)
    await _edit_panel(callback, text, kb)


@router.callback_query(F.data == "ap:unattr")
async def cb_unattributed(callback: CallbackQuery) -> None:
    """Show units with no known internal adder."""
    if not await _gate_admin(callback):
        return
    async with acquire_connection() as conn:
        text, kb = await _render_unattributed(conn)
    await _edit_panel(callback, text, kb)


@router.callback_query(F.data == "ap:bc")
async def cb_business(callback: CallbackQuery) -> None:
    """Show Business connection grants."""
    if not await _gate_admin(callback):
        return
    async with acquire_connection() as conn:
        text, kb = await _render_bc(conn)
    await _edit_panel(callback, text, kb)


@router.callback_query(F.data.startswith("ap:u:"))
async def cb_user(callback: CallbackQuery) -> None:
    """Drill into one connector's units. Payload: ``ap:u:<internal_user_uuid>``."""
    if not await _gate_admin(callback):
        return
    raw = (callback.data or "").removeprefix("ap:u:")
    try:
        internal_user_id = UUID(raw)
    except ValueError:
        await callback.answer()
        return
    async with acquire_connection() as conn:
        text, kb = await _render_user(conn, internal_user_id)
    await _edit_panel(callback, text, kb)
