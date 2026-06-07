-- =============================================================================
-- 0009_chat_processing_cursor.sql
-- Per-chat Tier-2 analysis watermark.
--
-- Phase 9 (batch analysis). The unified analyze_chat worker processes the
-- messages of a chat that arrived AFTER this timestamp, then advances it to the
-- newest message it handled — so each message is analysed once and the LLM always
-- sees a fresh window. NULL = never processed (the first run takes everything in
-- the window).
--
-- Idempotent: ADD COLUMN IF NOT EXISTS. Numbered 0009 — 0008 is the latest
-- applied migration (schema_migrations is empty; manual-apply path).
-- =============================================================================

BEGIN;

ALTER TABLE chats
    ADD COLUMN IF NOT EXISTS last_processed_at TIMESTAMPTZ;

COMMIT;
