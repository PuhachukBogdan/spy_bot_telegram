-- =============================================================================
-- 0006_topics_and_business.sql
-- Telegram Business mode support + unit typing.
--
-- The forum-topic columns (message_thread_id / topic_name / topic_key) and the
-- per-topic UNIQUE(telegram_chat_id, topic_key) were ALREADY shipped in
-- 0005_topic_units.sql and are intentionally NOT repeated here. This migration
-- adds only the net-new pieces:
--   * chats.unit_type discriminator (group / topic / business) + business binding
--   * messages business provenance + soft-deletion record
--   * four new tables: business_connections, partner_contacts, notes, reminders
--
-- For a business unit, chats.telegram_chat_id holds the PARTNER's Telegram
-- user_id: a Telegram private chat has chat.id == user.id, so the existing
-- (telegram_chat_id, topic_key) uniqueness keeps business DMs distinct with
-- topic_key 0.
--
-- Idempotent: re-running is a no-op (IF [NOT] EXISTS guards). No seed data.
-- =============================================================================

BEGIN;

-- gen_random_uuid(); already enabled by 0001, guarded here per request.
CREATE EXTENSION IF NOT EXISTS pgcrypto;

-- ---------------------------------------------------------------------------
-- 1. chats: unit typing + business binding.
--    Topic columns + the (telegram_chat_id, topic_key) UNIQUE come from 0005.
-- ---------------------------------------------------------------------------
ALTER TABLE chats ADD COLUMN IF NOT EXISTS unit_type TEXT NOT NULL DEFAULT 'group';  -- group / topic / business
ALTER TABLE chats ADD COLUMN IF NOT EXISTS business_connection_id TEXT;
ALTER TABLE chats ADD COLUMN IF NOT EXISTS business_peer_user_id BIGINT;

-- ---------------------------------------------------------------------------
-- 2. messages: business provenance + soft-deletion record.
--    `source` stays free-text TEXT (no CHECK constraint in 0001), so it already
--    accepts live_group / live_topic / business / imported with no change; the
--    application layer is responsible for writing those values.
-- ---------------------------------------------------------------------------
ALTER TABLE messages ADD COLUMN IF NOT EXISTS business_connection_id TEXT;
ALTER TABLE messages ADD COLUMN IF NOT EXISTS business_peer_user_id BIGINT;
ALTER TABLE messages ADD COLUMN IF NOT EXISTS deleted_at TIMESTAMPTZ;
ALTER TABLE messages ADD COLUMN IF NOT EXISTS deletion_payload JSONB;

-- ---------------------------------------------------------------------------
-- 3. business_connections — one row per Telegram Business connection grant.
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS business_connections (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    business_connection_id TEXT UNIQUE NOT NULL,
    business_account_user_id BIGINT NOT NULL,
    internal_user_id UUID REFERENCES internal_users(id),
    status TEXT NOT NULL DEFAULT 'pending',  -- pending / active / revoked / disabled
    rights JSONB NOT NULL DEFAULT '{}'::jsonb,
    connected_at TIMESTAMPTZ NOT NULL,
    revoked_at TIMESTAMPTZ,
    approved_by UUID REFERENCES internal_users(id),
    approved_at TIMESTAMPTZ,
    raw_payload JSONB,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS idx_business_connections_status ON business_connections(status);
CREATE INDEX IF NOT EXISTS idx_business_connections_account ON business_connections(business_account_user_id);

-- ---------------------------------------------------------------------------
-- 4. partner_contacts — map a Telegram user_id to a partner (for business DMs).
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS partner_contacts (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    partner_id UUID NOT NULL REFERENCES partners(id),
    telegram_user_id BIGINT UNIQUE NOT NULL,
    full_name TEXT,
    notes TEXT,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS idx_partner_contacts_partner ON partner_contacts(partner_id);

-- ---------------------------------------------------------------------------
-- 5. notes — internal staff notes per partner / chat.
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS notes (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    partner_id UUID NOT NULL REFERENCES partners(id),
    chat_id UUID REFERENCES chats(id),
    note_type TEXT NOT NULL,  -- general / handoff / open_question
    content TEXT NOT NULL,
    created_by UUID NOT NULL REFERENCES internal_users(id),
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    resolved_at TIMESTAMPTZ,
    resolved_by UUID REFERENCES internal_users(id)
);
CREATE INDEX IF NOT EXISTS idx_notes_partner_resolved ON notes(partner_id, resolved_at);

-- ---------------------------------------------------------------------------
-- 6. reminders — scheduled DM nudges to internal users.
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS reminders (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    target_user_id UUID NOT NULL REFERENCES internal_users(id),
    partner_id UUID REFERENCES partners(id),
    content TEXT NOT NULL,
    fire_at TIMESTAMPTZ NOT NULL,
    status TEXT NOT NULL DEFAULT 'pending',  -- pending / sent / cancelled / failed
    created_by UUID NOT NULL REFERENCES internal_users(id),
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    sent_at TIMESTAMPTZ
);
CREATE INDEX IF NOT EXISTS idx_reminders_pending ON reminders(status, fire_at) WHERE status = 'pending';

-- ---------------------------------------------------------------------------
-- RLS: match the backend-safe posture from 0004 (ENABLE, no policies; the bot
-- connects as service_role / postgres, both BYPASSRLS). Re-running is a no-op.
-- ---------------------------------------------------------------------------
ALTER TABLE business_connections ENABLE ROW LEVEL SECURITY;
ALTER TABLE partner_contacts ENABLE ROW LEVEL SECURITY;
ALTER TABLE notes ENABLE ROW LEVEL SECURITY;
ALTER TABLE reminders ENABLE ROW LEVEL SECURITY;

COMMIT;
