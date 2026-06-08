-- Migration 0011: manager activity signals
--
-- Stores LLM-detected manager proposals and closed deals per chat message.
-- Used by the manager-centric weekly/monthly summary (Phase 16) to count
-- how often each manager makes proposals and how many deals they close.
--
-- Attribution at query time: sender_id (Telegram user ID) →
--   internal_users.telegram_accounts @> jsonb_build_array(sender_id)
-- No denormalization — the join is cheap and keeps manager data current.

CREATE TABLE IF NOT EXISTS activity_signals (
    id          UUID        PRIMARY KEY DEFAULT gen_random_uuid(),
    chat_id     UUID        REFERENCES chats(id) ON DELETE CASCADE,
    message_id  UUID        REFERENCES messages(id) ON DELETE SET NULL,
    sender_id   BIGINT,                                         -- Telegram user ID
    signal_type TEXT        NOT NULL
        CHECK (signal_type IN ('manager_proposal', 'deal_closed')),
    description TEXT,                                           -- brief LLM explanation
    created_at  TIMESTAMPTZ NOT NULL DEFAULT now()
);

-- Primary access pattern: summary aggregates per sender per period
CREATE INDEX IF NOT EXISTS idx_activity_signals_sender_created
    ON activity_signals (sender_id, created_at);

-- Secondary: per-chat history view
CREATE INDEX IF NOT EXISTS idx_activity_signals_chat_created
    ON activity_signals (chat_id, created_at);
