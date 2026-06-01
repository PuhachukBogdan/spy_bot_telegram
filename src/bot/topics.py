"""Forum-topic helpers shared by the whitelist middleware and handlers.

A monitored unit is keyed by ``(telegram_chat_id, topic)`` where ``topic`` is the
forum topic id or ``None`` for the whole group. ``message_thread_id`` alone is
not enough to identify a topic: in non-forum supergroups it is also set for
reply-threads / linked-channel discussion threads, which are NOT separate
partners. So a message counts as belonging to a topic only when the chat is a
forum AND Telegram flags the message as a topic message.
"""

from __future__ import annotations

from aiogram.types import Message


def effective_topic_id(message: Message) -> int | None:
    """Return the forum topic id for a message, or ``None`` for group-level.

    ``None`` covers non-forum groups, the forum General topic, and all service
    messages (which are not topic messages) — they all map to the group-level
    unit (``topic_key = 0``).
    """
    if message.chat.is_forum and message.is_topic_message:
        return message.message_thread_id
    return None
