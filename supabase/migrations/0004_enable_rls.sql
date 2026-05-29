-- =============================================================================
-- 0004_enable_rls.sql
-- Phase 2 — strict backend-safe Row Level Security.
--
-- Enables RLS on all 17 public tables and adds NO policies. With RLS on and no
-- policy present, the deny-by-default rule applies: the `anon` and
-- `authenticated` roles (the public PostgREST API) can read/write nothing.
--
-- The bot is unaffected. It reaches Postgres only via:
--   - a direct asyncpg connection as the `postgres` role (SUPABASE_DB_URL), and
--   - the `service_role` key.
-- Both roles carry BYPASSRLS, so they ignore RLS entirely and keep full access.
--
-- This migration:
--   - adds no anon/authenticated policies,
--   - grants no public read/write,
--   - leaves access to backend/service-role operations only,
--   - is idempotent: ENABLE ROW LEVEL SECURITY on an already-enabled table is a
--     no-op, so the whole file is safe to re-run.
-- =============================================================================

BEGIN;

ALTER TABLE public.internal_users            ENABLE ROW LEVEL SECURITY;
ALTER TABLE public.partners                  ENABLE ROW LEVEL SECURITY;
ALTER TABLE public.chats                     ENABLE ROW LEVEL SECURITY;
ALTER TABLE public.messages                  ENABLE ROW LEVEL SECURITY;
ALTER TABLE public.message_edits             ENABLE ROW LEVEL SECURITY;
ALTER TABLE public.chat_events               ENABLE ROW LEVEL SECURITY;
ALTER TABLE public.risk_events               ENABLE ROW LEVEL SECURITY;
ALTER TABLE public.red_flag_patterns         ENABLE ROW LEVEL SECURITY;
ALTER TABLE public.summaries                 ENABLE ROW LEVEL SECURITY;
ALTER TABLE public.summaries_skipped         ENABLE ROW LEVEL SECURITY;
ALTER TABLE public.critical_alert_recipients ENABLE ROW LEVEL SECURITY;
ALTER TABLE public.admin_audit_log           ENABLE ROW LEVEL SECURITY;
ALTER TABLE public.llm_calls                 ENABLE ROW LEVEL SECURITY;
ALTER TABLE public.prompts                   ENABLE ROW LEVEL SECURITY;
ALTER TABLE public.failed_alerts             ENABLE ROW LEVEL SECURITY;
ALTER TABLE public.cost_tracking             ENABLE ROW LEVEL SECURITY;
ALTER TABLE public.processing_queue          ENABLE ROW LEVEL SECURITY;

COMMIT;
