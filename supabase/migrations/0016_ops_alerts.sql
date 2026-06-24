-- =============================================================================
-- 0016_ops_alerts.sql
-- Ops Alerts subsystem — payment-provider incident monitor + state.
--
-- Two new tables, independent of the monitoring pipeline:
--   - payment_incidents          : one row per external incident (dedup key)
--   - payment_incident_messages  : incident -> Telegram messages we posted in
--                                   each group (for editMessageText on updates)
--
-- RLS is enabled with no policies (deny-by-default), matching 0004: the bot
-- reaches these via the postgres / service_role BYPASSRLS roles only.
--
-- NOTE: This is the ONLY subsystem that proactively writes into partner groups
-- (sanctioned exception to the "bot never writes in partner chats" rule —
-- see CLAUDE.md §1).
-- =============================================================================

BEGIN;

-- One row per external incident. incident_id is the stable dedup key derived
-- from the feed item link; we UPSERT on it.
CREATE TABLE IF NOT EXISTS payment_incidents (
    incident_id  TEXT        PRIMARY KEY,
    country      TEXT        NOT NULL,
    provider     TEXT        NOT NULL,
    issue        TEXT,
    link         TEXT,
    details      TEXT,
    status       TEXT        NOT NULL,
    iso_date     TIMESTAMPTZ,
    last_update  TIMESTAMPTZ NOT NULL,
    -- true while the incident has been recorded but intentionally NOT broadcast
    -- (first-run seeding: everything already active at startup is marked seen
    -- so the bot does not blast history into every group on deploy).
    seeded_only  BOOLEAN     NOT NULL DEFAULT false,
    created_at   TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS idx_payment_incidents_status
    ON payment_incidents (status);
CREATE INDEX IF NOT EXISTS idx_payment_incidents_last_update
    ON payment_incidents (last_update DESC);

-- Link incident -> the Telegram message we sent in each group, so a later
-- update can edit it in place. chat_id references chats.id (UUID); the actual
-- telegram_message_id is the per-chat message to edit.
CREATE TABLE IF NOT EXISTS payment_incident_messages (
    incident_id         TEXT        NOT NULL
        REFERENCES payment_incidents(incident_id) ON DELETE CASCADE,
    chat_id             UUID        NOT NULL REFERENCES chats(id),
    telegram_message_id BIGINT      NOT NULL,
    posted_at           TIMESTAMPTZ NOT NULL DEFAULT now(),
    last_edited_at      TIMESTAMPTZ,
    edit_failed         BOOLEAN     NOT NULL DEFAULT false,
    edit_failure_reason TEXT,
    PRIMARY KEY (incident_id, chat_id)
);
CREATE INDEX IF NOT EXISTS idx_incident_messages_chat
    ON payment_incident_messages (chat_id);

-- One row per holiday we've reminded about, so the daily reminder fires exactly
-- once per holiday — restart-safe and tick-idempotent (no double posts).
CREATE TABLE IF NOT EXISTS ops_holiday_sends (
    holiday_date DATE        NOT NULL,
    holiday_name TEXT        NOT NULL,
    sent_at      TIMESTAMPTZ NOT NULL DEFAULT now(),
    PRIMARY KEY (holiday_date, holiday_name)
);

ALTER TABLE public.payment_incidents         ENABLE ROW LEVEL SECURITY;
ALTER TABLE public.payment_incident_messages ENABLE ROW LEVEL SECURITY;
ALTER TABLE public.ops_holiday_sends         ENABLE ROW LEVEL SECURITY;

COMMIT;
