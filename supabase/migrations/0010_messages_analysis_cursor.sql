-- =============================================================================
-- 0010_messages_analysis_cursor.sql
-- Tier-2 analysis cursor hardening.
--
-- Phase 9.1. The unified analyze_chat worker used messages.timestamp (the
-- Telegram send-time) as its per-chat watermark. That has two holes: it is only
-- second-granular (two messages in the same second at the watermark boundary can
-- be skipped forever), and it is immutable on edit (a message edited to add a
-- risk phrase after the watermark already passed it could never be re-analysed).
-- The cursor now tracks messages.created_at instead — ingestion time, which is
-- microsecond, monotonic, and bumpable to now() on a qualifying edit, so neither
-- hole remains.
--
-- created_at already exists and is populated for every row, so this migration
-- only adds the supporting index and re-parks the watermark onto the new scale.
--
-- Idempotent. Numbered 0010 — 0009 is the latest applied (schema_migrations is
-- empty; manual-apply path).
-- =============================================================================

BEGIN;

-- Serves the window query: one chat's messages with created_at past the cursor.
CREATE INDEX IF NOT EXISTS idx_messages_chat_created
    ON messages (chat_id, created_at);

-- Scale switch: the old watermark held a Telegram send-time; the cursor is now
-- created_at (ingestion time). Park already-watermarked chats at now() so the
-- switch does not re-analyse their history. NULL (never-processed) chats are left
-- untouched and take their normal first pass.
UPDATE chats SET last_processed_at = now() WHERE last_processed_at IS NOT NULL;

COMMIT;
