-- 0023_archive_import.sql
-- Support for importing partner-chat history from Telegram Desktop HTML exports
-- (the AFFS_CHATS archive: 241 aff_id folders, 57 372 messages, 2025-02 … 2026-08).
--
-- Additive and idempotent. Three concerns, in order of how badly each would bite:
--
-- 1. RETENTION WOULD DELETE THE ARCHIVE. `purge_old_data()` drops
--    `messages WHERE created_at < now() - 120 days`, and the archive is dated
--    2025-02 onward, so the pg_cron job `purge-old-data` (Sundays 03:00 UTC)
--    would erase almost all of it on its first run after the import. The purge is
--    meant to bound *live monitoring* growth, not to expire a history set that
--    was deliberately loaded, so every delete below is narrowed to
--    `source <> 'imported'`. Imported rows are now retained indefinitely; they are
--    a fixed ~30-60 MB that does not grow with traffic.
--
--    NOTE the predicate excludes the one archive value instead of allow-listing
--    live ones. `messages.source` records the delivery path — 'live_group',
--    'live_topic', 'business', plus bare 'live' on six pre-0006 rows — so
--    `source = 'live'` matches almost nothing (3 418 of 3 427 prod rows are
--    'live_group'). Writing it that way here would have made the purge a no-op
--    and, in the analysis window, would have hidden nearly every real message.
--
-- 2. UNMATCHED EXPORTS NEED A HOME. Roughly half the archive folders have no
--    `chats` row (the bot was never added to those groups). They are imported as
--    `status = 'archived'` units. Every live query filters positively on
--    `status = 'active'` (or `IN ('active','pending')`), so `archived` is
--    invisible to reports, the daily digest, ops-alert broadcasts and manager
--    attribution without touching any of them.
--
--    `telegram_chat_id` is NOT NULL and the export does not carry a chat id, so
--    such units get a synthetic placeholder. See `archive_placeholder_chat_id()`.
--
-- 3. HISTORY MUST RE-ATTACH LATER. When the bot is eventually added to one of
--    those groups, the archived unit has to merge into the real one, so the
--    aff_id it came from is recorded and indexed.
--
-- The matching live-pipeline lockout is NOT in this migration: it lives in
-- `get_chat_analysis_window`, which now skips `source = 'imported'` rows. That
-- is what stops 57k imported messages from being fed to the LLM — a watermark is
-- not enough, because 16 active chats have `last_processed_at IS NULL`, which
-- makes every message in them "new".

BEGIN;

-- --------------------------------------------------------------------------
-- 1. Provenance + re-attachment key on chats
-- --------------------------------------------------------------------------

-- Mirrors the spirit of messages.source, which has existed since 0001. On `chats`
-- only two values are meaningful ('live' / 'imported') — there is no per-delivery-
-- path distinction to record for a unit.
ALTER TABLE chats ADD COLUMN IF NOT EXISTS source TEXT NOT NULL DEFAULT 'live';

-- The archive folder (affiliate id) this unit was imported from. NULL for every
-- live unit. Not unique: one export can legitimately be split across units, and
-- one chat can serve several aff_ids ("LEGENDS | Betonwin | 58329 | 71862 | 74849").
ALTER TABLE chats ADD COLUMN IF NOT EXISTS import_aff_id TEXT;

COMMENT ON COLUMN chats.source IS
  'live = onboarded through Telegram; imported = created by the archive importer.';
COMMENT ON COLUMN chats.import_aff_id IS
  'Archive folder (aff_id) this unit was imported from; drives re-attachment when '
  'the bot is later added to the real group.';

-- Drives the auto-attach lookup on onboarding (find an archived unit by aff_id).
CREATE INDEX IF NOT EXISTS idx_chats_import_aff_id
    ON chats (import_aff_id)
 WHERE import_aff_id IS NOT NULL;

-- Cheap filter for "show me only imported units".
CREATE INDEX IF NOT EXISTS idx_chats_source_status
    ON chats (source, status)
 WHERE source <> 'live';

-- Imported history is queried by (chat, time) for the one-off retro analysis and
-- for the archive report; the live path never touches these rows.
CREATE INDEX IF NOT EXISTS idx_messages_imported
    ON messages (chat_id, "timestamp")
 WHERE source = 'imported';

-- --------------------------------------------------------------------------
-- 2. Synthetic chat ids for archive-only units
-- --------------------------------------------------------------------------
-- Real Telegram ids are bounded well under 1e13 in magnitude (supergroups are
-- ~-100xxxxxxxxxx). Placeholders are allocated from -9e15 downward: comfortably
-- inside bigint, impossible to collide with a real id, and obviously synthetic
-- when read by a human. Uniqueness is enforced by the existing
-- chats_chat_topic_key constraint on (telegram_chat_id, topic_key).
CREATE OR REPLACE FUNCTION archive_placeholder_chat_id(aff_id TEXT)
RETURNS BIGINT
LANGUAGE sql
IMMUTABLE
AS $$
    -- Deterministic in aff_id, so re-running the importer maps the same folder to
    -- the same placeholder and the ON CONFLICT path stays idempotent.
    --
    -- `::bit(32)::int` is the standard hex→integer idiom and yields a *signed*
    -- int32; the +2^31 shift maps it to [0, 2^32-1] so the result always moves
    -- downward from the base. Range: [-9000004294967295, -9000000000000000],
    -- three orders of magnitude inside bigint's floor (~-9.22e18).
    SELECT -9000000000000000::bigint
         - ((('x' || substr(md5(aff_id), 1, 8))::bit(32)::int)::bigint + 2147483648)
$$;

COMMENT ON FUNCTION archive_placeholder_chat_id(TEXT) IS
  'Deterministic stand-in telegram_chat_id for an archive-only unit (no real chat '
  'id exists in a Telegram HTML export). Far outside the real Telegram id range.';

-- --------------------------------------------------------------------------
-- 3. Retention: never purge imported history
-- --------------------------------------------------------------------------
-- Same body as 0021 except every messages-derived delete is scoped to live rows.
-- Kept as a full redefinition (not a patch) so the FK-safe ordering stays readable
-- in one place.
CREATE OR REPLACE FUNCTION purge_old_data(retention_days int default 120)
RETURNS TABLE(table_name text, deleted bigint)
LANGUAGE plpgsql
AS $$
declare
  cutoff timestamptz := now() - make_interval(days => retention_days);
  n bigint;
begin
  -- Imported history is exempt everywhere below: `messages.source <> 'imported'`.

  -- 1. failed_alerts -> risk_events (child first)
  delete from failed_alerts fa
   where fa.created_at < cutoff
      or fa.risk_event_id in (
           select id from risk_events
            where created_at < cutoff
               or message_id in (
                    select id from messages
                     where created_at < cutoff and source <> 'imported'));
  get diagnostics n = row_count; table_name := 'failed_alerts'; deleted := n; return next;

  -- 2. risk_events (releases its message_id refs)
  delete from risk_events
   where created_at < cutoff
      or message_id in (
           select id from messages where created_at < cutoff and source <> 'imported');
  get diagnostics n = row_count; table_name := 'risk_events'; deleted := n; return next;

  -- 3. remaining children of messages (NO ACTION FKs)
  delete from message_edits
   where created_at < cutoff
      or message_id in (
           select id from messages where created_at < cutoff and source <> 'imported');
  get diagnostics n = row_count; table_name := 'message_edits'; deleted := n; return next;

  delete from analyzed_file_hashes
   where created_at < cutoff
      or message_id in (
           select id from messages where created_at < cutoff and source <> 'imported');
  get diagnostics n = row_count; table_name := 'analyzed_file_hashes'; deleted := n; return next;

  delete from activity_signals where created_at < cutoff;  -- message_id is SET NULL anyway
  get diagnostics n = row_count; table_name := 'activity_signals'; deleted := n; return next;

  -- 4. analysis telemetry
  delete from llm_calls where created_at < cutoff;
  get diagnostics n = row_count; table_name := 'llm_calls'; deleted := n; return next;

  delete from processing_queue where created_at < cutoff;
  get diagnostics n = row_count; table_name := 'processing_queue'; deleted := n; return next;

  -- 5. messages — LIVE ONLY. Imported archive rows are kept indefinitely.
  delete from messages where created_at < cutoff and source <> 'imported';
  get diagnostics n = row_count; table_name := 'messages'; deleted := n; return next;

  -- 6. misc telemetry / audit
  delete from chat_events where created_at < cutoff;
  get diagnostics n = row_count; table_name := 'chat_events'; deleted := n; return next;

  delete from admin_audit_log where created_at < cutoff;
  get diagnostics n = row_count; table_name := 'admin_audit_log'; deleted := n; return next;

  -- 7. report artefacts past the retention window (see 0021 for the reasoning).
  delete from summaries where created_at < cutoff;
  get diagnostics n = row_count; table_name := 'summaries'; deleted := n; return next;

  delete from dashboards where created_at < cutoff;
  get diagnostics n = row_count; table_name := 'dashboards'; deleted := n; return next;

  return;
end;
$$;

COMMENT ON FUNCTION purge_old_data(int) IS
  'Delete LIVE monitoring rows older than retention_days (default 120). FK-safe '
  'order. messages.source = ''imported'' is never purged. Returns per-table counts.';

COMMIT;
