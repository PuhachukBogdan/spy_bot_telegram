-- 0021_retention_cleanup.sql
-- Data-retention purge: drop monitoring rows older than N days (default 120 = ~4 months).
--
-- Rationale: `messages` (and its downstream telemetry) is the only unbounded grower.
-- Nobody needs raw chat history beyond a few months, so a single retention window keeps
-- the DB flat and well under Supabase's 500 MB free-tier cap indefinitely.
--
-- Deletes run in FK-safe order (children before parents). All FKs onto `messages`
-- (risk_events, message_edits, analyzed_file_hashes) are NO ACTION and would otherwise
-- block the parent delete, so each is cleared first; activity_signals.message_id is
-- SET NULL and needs no pre-step.
--
-- KEPT (config / operational / tiny — never grows with traffic):
--   chats, partners, internal_users, red_flag_patterns, prompts, suppression_rules,
--   business_connections, payment_incidents, notes, reminders.
--
-- NOTE: DELETE marks tuples dead; autovacuum reuses that space for new inserts (steady
-- state stays flat). It does NOT shrink the file back to the OS — that needs VACUUM FULL
-- (exclusive lock, run manually if you ever must reclaim to disk). For free-tier headroom
-- the reuse behaviour is what matters, so no VACUUM is issued here.

create or replace function purge_old_data(retention_days int default 120)
returns table(table_name text, deleted bigint)
language plpgsql
as $$
declare
  cutoff timestamptz := now() - make_interval(days => retention_days);
  n bigint;
begin
  -- 1. failed_alerts -> risk_events (child first)
  delete from failed_alerts fa
   where fa.created_at < cutoff
      or fa.risk_event_id in (
           select id from risk_events
            where created_at < cutoff
               or message_id in (select id from messages where created_at < cutoff));
  get diagnostics n = row_count; table_name := 'failed_alerts'; deleted := n; return next;

  -- 2. risk_events (releases its message_id refs)
  delete from risk_events
   where created_at < cutoff
      or message_id in (select id from messages where created_at < cutoff);
  get diagnostics n = row_count; table_name := 'risk_events'; deleted := n; return next;

  -- 3. remaining children of messages (NO ACTION FKs)
  delete from message_edits
   where created_at < cutoff
      or message_id in (select id from messages where created_at < cutoff);
  get diagnostics n = row_count; table_name := 'message_edits'; deleted := n; return next;

  delete from analyzed_file_hashes
   where created_at < cutoff
      or message_id in (select id from messages where created_at < cutoff);
  get diagnostics n = row_count; table_name := 'analyzed_file_hashes'; deleted := n; return next;

  delete from activity_signals where created_at < cutoff;  -- message_id is SET NULL anyway
  get diagnostics n = row_count; table_name := 'activity_signals'; deleted := n; return next;

  -- 4. analysis telemetry
  delete from llm_calls where created_at < cutoff;
  get diagnostics n = row_count; table_name := 'llm_calls'; deleted := n; return next;

  delete from processing_queue where created_at < cutoff;
  get diagnostics n = row_count; table_name := 'processing_queue'; deleted := n; return next;

  -- 5. messages (safe now — no inbound refs remain)
  delete from messages where created_at < cutoff;
  get diagnostics n = row_count; table_name := 'messages'; deleted := n; return next;

  -- 6. misc telemetry / audit
  delete from chat_events where created_at < cutoff;
  get diagnostics n = row_count; table_name := 'chat_events'; deleted := n; return next;

  delete from admin_audit_log where created_at < cutoff;
  get diagnostics n = row_count; table_name := 'admin_audit_log'; deleted := n; return next;

  -- 7. report artefacts past the retention window. Share tokens expire on their
  --    own 7d/30d schedule (killing the LINK); here we drop the stored rows only
  --    after `cutoff`, so report history stays browsable/regenerable for the full
  --    retention window like everything else — not deleted the moment a link dies.
  delete from summaries where created_at < cutoff;
  get diagnostics n = row_count; table_name := 'summaries'; deleted := n; return next;

  delete from dashboards where created_at < cutoff;
  get diagnostics n = row_count; table_name := 'dashboards'; deleted := n; return next;

  return;
end;
$$;

comment on function purge_old_data(int) is
  'Delete monitoring rows older than retention_days (default 120). FK-safe order. Returns per-table delete counts.';

-- ---------------------------------------------------------------------------
-- Scheduling (choose ONE)
-- ---------------------------------------------------------------------------
-- Option A — pg_cron (DB-native, runs even when the app is down). PREFERRED.
--   Enable the extension once (Supabase Dashboard -> Database -> Extensions -> pg_cron,
--   or, if your role allows: `create extension if not exists pg_cron;`), then:
--
--     select cron.schedule(
--       'purge-old-data',
--       '0 3 * * 0',                       -- every Sunday 03:00 UTC
--       $$ select purge_old_data(120) $$
--     );
--
--   Inspect / remove:  select * from cron.job;   select cron.unschedule('purge-old-data');
--
-- Option B — OS cron on the app server calling the runner script:
--     0 3 * * 0  cd /path/to/app && .venv/bin/python scripts/cleanup_retention.py --days 120
-- ---------------------------------------------------------------------------
