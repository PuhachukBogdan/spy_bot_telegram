-- =============================================================================
-- 0008_work_hours.sql
-- Per-manager working hours + timezone on internal_users.
--
-- Feeds the operational_sla track (CLAUDE.md 7.6, risk-architecture 2026-06-07):
-- a partner message left unanswered beyond the SLA threshold *during the owning
-- manager's working hours* raises an operational_sla risk. The hours are the
-- manager's own (set via /set_hours), so they live on internal_users.
--
-- All three columns are nullable / defaulted so existing rows stay valid:
--   * work_hours_start / work_hours_end — NULL means "not set yet"; the SLA job
--     skips a manager with no hours rather than guessing.
--   * work_timezone — NOT NULL DEFAULT 'UTC' so the column is always usable; an
--     IANA name string (e.g. 'Europe/Kiev'), validated in app code on write.
--
-- Idempotent: ADD COLUMN IF NOT EXISTS (safe to re-run). Numbered 0008 — 0007 is
-- the latest applied migration (schema_migrations is empty; manual-apply path).
-- =============================================================================

BEGIN;

ALTER TABLE internal_users
    ADD COLUMN IF NOT EXISTS work_hours_start TIME,
    ADD COLUMN IF NOT EXISTS work_hours_end   TIME,
    ADD COLUMN IF NOT EXISTS work_timezone    TEXT NOT NULL DEFAULT 'UTC';

COMMIT;
