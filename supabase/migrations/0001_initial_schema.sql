-- =============================================================================
-- 0001_initial_schema.sql
-- Phase 2 — full schema for the Telegram partner-chat risk monitor.
-- All 17 tables from CLAUDE.md section 5, in foreign-key-safe creation order,
-- with every index from section 5 declared inline after its table.
-- =============================================================================

BEGIN;

-- --- Extensions -------------------------------------------------------------
-- pgcrypto provides gen_random_uuid() (built into core on PG13+, but enabling
-- the extension is harmless and keeps the migration portable).
CREATE EXTENSION IF NOT EXISTS pgcrypto;

-- pgvector supplies the `vector` type for Phase 2 RAG embeddings.
-- NOTE: the extension *identifier* is `vector`. The project is called
-- "pgvector", but `CREATE EXTENSION pgvector` does not exist and would fail.
CREATE EXTENSION IF NOT EXISTS vector;

-- =============================================================================
-- 1. internal_users  (no FK dependencies — created first; partners + chats
--    reference it)
-- =============================================================================
CREATE TABLE internal_users (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    full_name TEXT NOT NULL,
    role TEXT,
    telegram_accounts JSONB NOT NULL DEFAULT '[]'::jsonb, -- array of user_id (may be several)
    is_admin BOOLEAN NOT NULL DEFAULT false,
    enabled BOOLEAN NOT NULL DEFAULT true,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now()
);
-- GIN index for lookups by user_id inside the jsonb array.
CREATE INDEX idx_internal_users_tg ON internal_users USING GIN (telegram_accounts);

-- =============================================================================
-- 2. partners  (FK: owner_manager_id -> internal_users)
-- =============================================================================
CREATE TABLE partners (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    name TEXT NOT NULL UNIQUE,
    status TEXT NOT NULL DEFAULT 'active', -- active / passive / risky / inactive
    owner_manager_id UUID REFERENCES internal_users(id),
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT now()
);

-- =============================================================================
-- 3. chats  (FK: partner_id -> partners, authorized_by -> internal_users)
-- =============================================================================
CREATE TABLE chats (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    telegram_chat_id BIGINT NOT NULL UNIQUE, -- negative for groups/supergroups
    partner_id UUID REFERENCES partners(id),
    chat_name TEXT,
    status TEXT NOT NULL DEFAULT 'pending', -- pending / active / abandoned / inactive / banned
    added_by_user_id BIGINT,                -- TG user_id of whoever added the bot
    authorized_by UUID REFERENCES internal_users(id),
    authorized_at TIMESTAMPTZ,
    chat_purpose TEXT,                       -- operations / finance / tech / general (optional)
    created_at TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX idx_chats_status ON chats(status);
CREATE INDEX idx_chats_partner ON chats(partner_id);

-- =============================================================================
-- 4. messages  (FK: chat_id -> chats)
-- =============================================================================
CREATE TABLE messages (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    telegram_message_id BIGINT NOT NULL,
    chat_id UUID NOT NULL REFERENCES chats(id),
    sender_id BIGINT,
    sender_chat_id BIGINT,                   -- for anonymous admins
    sender_name TEXT,
    sender_role TEXT NOT NULL,               -- internal / partner / anonymous_admin / unknown
    message_text TEXT,
    message_type TEXT NOT NULL,              -- text / voice / video_note / document / photo / forward / etc.
    timestamp TIMESTAMPTZ NOT NULL,
    reply_to_message_id BIGINT,
    forward_from_id BIGINT,
    forward_from_chat_id BIGINT,
    message_thread_id BIGINT,                -- for forum topics
    links TEXT[],
    mentions TEXT[],
    detected_language TEXT,                  -- langdetect
    transcription TEXT,                      -- voice/video -> transcription text
    is_significant BOOLEAN DEFAULT false,    -- precomputed flag for summary filter
    has_triggers BOOLEAN DEFAULT false,      -- Tier 1 fired
    triggered_patterns JSONB,                -- which patterns matched
    base_score INT DEFAULT 0,
    source TEXT NOT NULL DEFAULT 'live',     -- live / imported
    raw_payload JSONB,                       -- full Telegram object
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    UNIQUE (chat_id, telegram_message_id)    -- idempotency
);
CREATE INDEX idx_messages_chat_time ON messages(chat_id, timestamp DESC);
CREATE INDEX idx_messages_triggers ON messages(chat_id, has_triggers) WHERE has_triggers = true;
CREATE INDEX idx_messages_sender ON messages(sender_id);

-- =============================================================================
-- 5. message_edits  (FK: message_id -> messages)
-- =============================================================================
CREATE TABLE message_edits (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    message_id UUID NOT NULL REFERENCES messages(id),
    old_text TEXT,
    new_text TEXT,
    edited_at TIMESTAMPTZ NOT NULL,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX idx_edits_message ON message_edits(message_id);

-- =============================================================================
-- 6. chat_events  (FK: chat_id -> chats)
-- =============================================================================
CREATE TABLE chat_events (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    chat_id UUID NOT NULL REFERENCES chats(id),
    event_type TEXT NOT NULL,         -- member_join / member_leave / title_change / migration / unknown_party_joined
    actor_user_id BIGINT,
    target_user_id BIGINT,
    payload JSONB,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX idx_chat_events_chat ON chat_events(chat_id, created_at DESC);

-- =============================================================================
-- 7. risk_events  (FK: message_id -> messages, partner_id -> partners,
--    chat_id -> chats, reviewed_by -> internal_users)
-- =============================================================================
CREATE TABLE risk_events (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    message_id UUID REFERENCES messages(id),
    partner_id UUID REFERENCES partners(id),
    chat_id UUID REFERENCES chats(id),
    sender_id BIGINT,
    risk_type TEXT NOT NULL,                  -- one of the 12 categories
    risk_level TEXT NOT NULL,                 -- low / medium / high / critical
    triggered_patterns JSONB,                 -- which phrases matched
    context_modifiers JSONB,                  -- {financial: +20, internal: +10, etc.}
    base_score INT NOT NULL,
    llm_confidence FLOAT,                      -- 0..1
    llm_multiplier FLOAT,                      -- 1.2 / 0.4 / 1.0
    llm_verdict TEXT,                          -- confirmed / likely_fp / uncertain
    llm_explanation TEXT,
    final_score INT NOT NULL,
    disagreement BOOLEAN DEFAULT false,        -- rule-based >=50 AND LLM <=20
    detected_phrase TEXT,
    context_message_ids UUID[],                -- messages in the window
    status TEXT NOT NULL DEFAULT 'new',        -- new / reviewed / confirmed / false_positive / escalated
    reviewed_by UUID REFERENCES internal_users(id),
    reviewed_at TIMESTAMPTZ,
    slack_message_ts TEXT,                     -- for dedup and threads
    created_at TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX idx_risk_events_partner ON risk_events(partner_id, created_at DESC);
CREATE INDEX idx_risk_events_level ON risk_events(risk_level, created_at DESC);
CREATE INDEX idx_risk_events_disagreement ON risk_events(disagreement) WHERE disagreement = true;

-- =============================================================================
-- 8. red_flag_patterns  (no FK dependencies)
-- =============================================================================
CREATE TABLE red_flag_patterns (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    pattern TEXT NOT NULL,                          -- literal or regex
    pattern_type TEXT NOT NULL DEFAULT 'literal',   -- literal / regex
    language TEXT NOT NULL,                          -- ru / en / es / pt / ua
    risk_category TEXT NOT NULL,                     -- one of the 12 categories
    base_score INT NOT NULL,
    examples TEXT[],
    enabled BOOLEAN NOT NULL DEFAULT true,
    updated_at TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX idx_patterns_enabled ON red_flag_patterns(enabled) WHERE enabled = true;

-- =============================================================================
-- 9. summaries  (FK: partner_id -> partners)
-- =============================================================================
CREATE TABLE summaries (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    partner_id UUID REFERENCES partners(id),
    period_type TEXT NOT NULL,                       -- weekly / monthly
    period_start TIMESTAMPTZ NOT NULL,
    period_end TIMESTAMPTZ NOT NULL,
    structured_content JSONB NOT NULL,               -- blocks per Table 9/10 of the spec
    rendered_html TEXT,
    risk_event_ids UUID[],
    action_items JSONB,
    delivery_status TEXT NOT NULL DEFAULT 'pending', -- pending / delivered / failed
    delivered_at TIMESTAMPTZ,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX idx_summaries_partner_period ON summaries(partner_id, period_type, period_start DESC);

-- =============================================================================
-- 10. summaries_skipped  (FK: partner_id -> partners)
-- =============================================================================
CREATE TABLE summaries_skipped (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    partner_id UUID REFERENCES partners(id),
    period_type TEXT NOT NULL,
    period_start TIMESTAMPTZ NOT NULL,
    period_end TIMESTAMPTZ NOT NULL,
    reason TEXT NOT NULL,                            -- insufficient_activity / etc.
    significant_message_count INT,
    risk_event_count INT,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now()
);

-- =============================================================================
-- 11. critical_alert_recipients  (no FK dependencies)
-- =============================================================================
CREATE TABLE critical_alert_recipients (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    full_name TEXT NOT NULL,
    slack_user_id TEXT,
    email TEXT,
    enabled BOOLEAN NOT NULL DEFAULT true,
    added_at TIMESTAMPTZ NOT NULL DEFAULT now()
);

-- =============================================================================
-- 12. admin_audit_log  (FK: actor_internal_id -> internal_users)
-- =============================================================================
CREATE TABLE admin_audit_log (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    actor_user_id BIGINT,                            -- TG user_id
    actor_internal_id UUID REFERENCES internal_users(id),
    action TEXT NOT NULL,                            -- authorize_chat / reject_chat / mark_fp / mark_confirmed / mark_escalated / etc.
    target_entity TEXT,                              -- chat / partner / risk_event
    target_id UUID,
    payload JSONB,
    ip TEXT,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX idx_audit_actor ON admin_audit_log(actor_internal_id, created_at DESC);
CREATE INDEX idx_audit_target ON admin_audit_log(target_entity, target_id);

-- =============================================================================
-- 13. llm_calls  (FK: chat_id -> chats)
-- =============================================================================
CREATE TABLE llm_calls (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    call_type TEXT NOT NULL,                         -- tier2_batch / priority / weekly_summary / monthly_summary
    model TEXT NOT NULL,
    chat_id UUID REFERENCES chats(id),
    message_ids UUID[],                              -- which messages were involved
    prompt_hash TEXT NOT NULL,                       -- SHA-256 of the prompt
    prompt_storage_path TEXT,                        -- path in Supabase Storage
    response_summary TEXT,                           -- short digest for quick scanning
    response_storage_path TEXT,
    tokens_in INT,
    tokens_out INT,
    cost_usd NUMERIC(10, 6),
    latency_ms INT,
    disagreement_flag BOOLEAN DEFAULT false,
    error TEXT,                                      -- set if the call failed
    created_at TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX idx_llm_calls_time ON llm_calls(created_at DESC);
CREATE INDEX idx_llm_calls_chat ON llm_calls(chat_id, created_at DESC);

-- =============================================================================
-- 14. prompts  (no FK dependencies)
-- =============================================================================
CREATE TABLE prompts (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    name TEXT NOT NULL,                              -- tier2_risk_analysis / weekly_summary / etc.
    version INT NOT NULL,
    template TEXT NOT NULL,
    json_schema JSONB,                               -- for structured output
    active BOOLEAN NOT NULL DEFAULT false,
    notes TEXT,
    created_by TEXT,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    UNIQUE (name, version)
);
CREATE INDEX idx_prompts_active ON prompts(name, active) WHERE active = true;

-- =============================================================================
-- 15. failed_alerts  (FK: risk_event_id -> risk_events)
-- =============================================================================
CREATE TABLE failed_alerts (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    risk_event_id UUID REFERENCES risk_events(id),
    channel TEXT NOT NULL,                           -- slack / telegram_management
    payload JSONB,
    error TEXT,
    retry_count INT NOT NULL DEFAULT 0,
    last_attempt_at TIMESTAMPTZ,
    resolved BOOLEAN NOT NULL DEFAULT false,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now()
);

-- =============================================================================
-- 16. cost_tracking  (no FK dependencies; total_cost_usd is generated)
-- =============================================================================
CREATE TABLE cost_tracking (
    date DATE NOT NULL PRIMARY KEY,
    llm_cost_usd NUMERIC(10, 4) NOT NULL DEFAULT 0,
    whisper_cost_usd NUMERIC(10, 4) NOT NULL DEFAULT 0,
    total_cost_usd NUMERIC(10, 4) GENERATED ALWAYS AS (llm_cost_usd + whisper_cost_usd) STORED,
    llm_calls_count INT NOT NULL DEFAULT 0,
    whisper_calls_count INT NOT NULL DEFAULT 0,
    circuit_breaker_triggered BOOLEAN DEFAULT false
);

-- =============================================================================
-- 17. processing_queue  (no FK dependencies; Postgres-as-queue)
-- =============================================================================
CREATE TABLE processing_queue (
    id BIGSERIAL PRIMARY KEY,
    task_type TEXT NOT NULL,                         -- whisper_transcribe / priority_llm / batch_llm
    payload JSONB NOT NULL,
    status TEXT NOT NULL DEFAULT 'pending',          -- pending / in_progress / done / failed
    attempts INT NOT NULL DEFAULT 0,
    last_attempt_at TIMESTAMPTZ,
    error TEXT,
    scheduled_for TIMESTAMPTZ NOT NULL DEFAULT now(),
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    completed_at TIMESTAMPTZ
);
-- Partial index drives the FOR UPDATE SKIP LOCKED dequeue path.
CREATE INDEX idx_queue_pending ON processing_queue(task_type, scheduled_for)
    WHERE status = 'pending';

COMMIT;
