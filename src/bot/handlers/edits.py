"""edited_message handler. Phase 5.

Telegram delivers edits on a separate update type (``edited_message``), so the
message-observer whitelist middleware does not gate these. We don't need it to:
an edit for a chat we never stored (non-active, or pre-activation) simply finds
no original row and is ignored.

On a real text change we append a ``message_edits`` history row and overwrite the
current text (CLAUDE.md 7.3). Re-running the Tier-1 matcher on the new text lands
in Phase 6.
"""

from __future__ import annotations

from datetime import UTC, datetime

from aiogram import F, Router
from aiogram.enums import ChatType
from aiogram.types import Message

from src.bot.topics import effective_topic_id
from src.config import settings
from src.db.client import acquire_connection
from src.db.queries.chats import get_chat_unit
from src.db.queries.messages import (
    bump_message_for_analysis,
    get_message,
    insert_message_edit,
    update_message_text,
    update_message_triggers,
)
from src.db.queries.queue import enqueue_chat_analysis
from src.pipeline.tier1 import pattern_cache
from src.utils.logging import get_logger

log = get_logger(__name__)

router = Router(name="edits")
router.edited_message.filter(F.chat.type.in_({ChatType.GROUP, ChatType.SUPERGROUP}))


@router.edited_message()
async def on_edited_message(edited: Message) -> None:
    """Record an edit to a previously-stored message."""
    new_text = edited.text if edited.text is not None else edited.caption

    async with acquire_connection() as conn:
        chat = await get_chat_unit(conn, edited.chat.id, effective_topic_id(edited))
        if chat is None:
            return
        existing = await get_message(conn, chat.id, edited.message_id)
        if existing is None:
            # We never stored the original (e.g. it predates activation). Nothing
            # to diff against; ignore rather than insert a partial row.
            return
        if new_text == existing.message_text:
            return  # non-text edit (media swap, etc.) or no real change

        # aiogram exposes edit_date as a Unix int (only `date` is auto-converted).
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

        # Re-run Tier-1 on the edited text (CLAUDE.md 7.3): an edit can introduce
        # or remove triggers. Only bump the chat's analysis task to run now if the
        # score newly crossed the threshold, to avoid re-queueing on every benign
        # edit (decision A — unified analyze_chat lane; priority is an earlier
        # scheduled_for, not a separate queue).
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
            await enqueue_chat_analysis(conn, chat.id, datetime.now(UTC))

    log.info(
        "edit.recorded",
        chat_id=edited.chat.id,
        msg_id=edited.message_id,
        has_triggers=result.has_triggers,
        base_score=result.base_score,
    )
