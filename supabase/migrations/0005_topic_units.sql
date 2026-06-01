-- =============================================================================
-- 0005_topic_units.sql
-- Topic separation — a `chats` row becomes a monitored *unit* = one forum topic,
-- not a whole supergroup (see wiki proj1-tgbot-topic-separation-plan).
--
-- Forum supergroups host many independent topics; each can be a different
-- partner. Because messages / chat_events / risk_events / summaries all FK to
-- chats.id, making a chats row represent a single topic separates the whole
-- pipeline downstream with no further schema change.
--
-- Changes (all additive; existing rows stay valid):
--   - message_thread_id: the Telegram forum topic id (NULL = whole group /
--     General topic / non-forum group).
--   - topic_name: best-effort display name of the topic (often NULL).
--   - topic_key: generated COALESCE(message_thread_id, 0) so NULL threads share
--     one key per supergroup; used for a NULL-safe uniqueness.
--   - uniqueness moves from (telegram_chat_id) to (telegram_chat_id, topic_key):
--     one row per (supergroup, topic). Current NULL-thread rows get topic_key 0,
--     so no data migration is needed.
--
-- Idempotent: re-running is a no-op (IF [NOT] EXISTS guards + constraint check).
-- =============================================================================

BEGIN;

ALTER TABLE chats ADD COLUMN IF NOT EXISTS message_thread_id BIGINT;
ALTER TABLE chats ADD COLUMN IF NOT EXISTS topic_name TEXT;
ALTER TABLE chats ADD COLUMN IF NOT EXISTS topic_key BIGINT
    GENERATED ALWAYS AS (COALESCE(message_thread_id, 0)) STORED;

-- Replace per-supergroup uniqueness with per-topic uniqueness.
ALTER TABLE chats DROP CONSTRAINT IF EXISTS chats_telegram_chat_id_key;

DO $$
BEGIN
    IF NOT EXISTS (
        SELECT 1 FROM pg_constraint WHERE conname = 'chats_chat_topic_key'
    ) THEN
        ALTER TABLE chats
            ADD CONSTRAINT chats_chat_topic_key
            UNIQUE (telegram_chat_id, topic_key);
    END IF;
END$$;

COMMIT;
