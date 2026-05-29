-- =============================================================================
-- 0002_seed_data.sql
-- Phase 2 — minimal seed.
--
-- Only the three prompt rows the pipeline expects to exist. Templates are left
-- empty on purpose; they are populated in Phase 8 (LLM client + prompt loader),
-- which falls back to prompts/*.txt while the DB template is empty.
--
-- Deliberately NOT seeded (entered by hand via Supabase Studio):
--   - internal_users   (the admin list)
--   - partners
--   - red_flag_patterns (the Tier 1 dictionary, 5 languages, by Analytics Team)
--   - critical_alert_recipients
-- =============================================================================

BEGIN;

-- Active v1 prompt rows. ON CONFLICT keeps this migration idempotent against
-- the UNIQUE (name, version) constraint if it is ever re-applied.
INSERT INTO prompts (name, version, template, json_schema, active, notes, created_by)
VALUES
    ('tier2_risk_analysis', 1, '', NULL, true,
     'Placeholder seeded in Phase 2; real template lands in Phase 8.', 'seed'),
    ('weekly_summary', 1, '', NULL, true,
     'Placeholder seeded in Phase 2; real template lands in Phase 8.', 'seed'),
    ('monthly_summary', 1, '', NULL, true,
     'Placeholder seeded in Phase 2; real template lands in Phase 8.', 'seed')
ON CONFLICT (name, version) DO NOTHING;

COMMIT;
