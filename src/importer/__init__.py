"""Telegram Desktop HTML-export ingestion (partner chat history archive).

One-off-ish import path, kept separate from the live pipeline: the archive has no
Telegram ``chat_id`` and no sender ``user_id``, so imported rows can never be
identity-joined the way live ingestion does. See ``parser`` for what the export
format does and does not carry.
"""
