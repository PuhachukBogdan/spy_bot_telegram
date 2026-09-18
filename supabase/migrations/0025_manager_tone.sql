-- 0025_manager_tone.sql
-- Tone of voice (Phase 2, track F): daily LLM review of how managers write to
-- partners — toxicity, completeness, courtesy/register, complaint handling,
-- initiative.
--
-- Deliberately NOT `risk_events`. A tone flag is an observation about an
-- employee's wording, on a different scale from company risk; anything in
-- risk_events with score >= 60 pages Slack, and a curt reply must never sit in the
-- same channel as a traffic-diversion alert. These tables are read by the Team
-- summary page only. Nothing here can raise an alert (CLAUDE.md §1, §18).
--
-- Three tables:
--   manager_tone_daily    additive counters per (manager, local day, metric). Not
--                         tied to messages, so they SURVIVE the 120-day retention
--                         purge and the page keeps its history.
--   manager_tone_flags    one row per accepted flag with the verbatim quote — the
--                         dossier's review list. FK onto messages with CASCADE: the
--                         purge takes the flag with the message; the counter stays.
--   manager_tone_progress one row per completed (chat, day) — idempotent, resumable
--                         daily pass + per-call cost accounting.
--
-- RLS on, no policies (service_role BYPASSRLS reaches it; matches 0004/0016/0024).

BEGIN;

CREATE TABLE IF NOT EXISTS manager_tone_daily (
    manager_id  UUID NOT NULL REFERENCES internal_users(id),
    -- Local calendar day in REPORT_TIMEZONE — the same calendar the trend
    -- buckets and the risk `day` use, so the period filter never disagrees.
    day         DATE NOT NULL,
    metric      TEXT NOT NULL,
    -- Messages the model flagged under this metric that day.
    flagged     INT  NOT NULL DEFAULT 0,
    -- Manager messages the model was asked to judge that day. Written on EVERY
    -- metric row of a manager-day (same value) — the zero-flag rows are the
    -- denominator, which is why they exist at all.
    assessed    INT  NOT NULL DEFAULT 0,
    updated_at  TIMESTAMPTZ NOT NULL DEFAULT now(),
    PRIMARY KEY (manager_id, day, metric)
);

COMMENT ON TABLE manager_tone_daily IS
  'Tone-of-voice counters per manager, local day and metric (flagged / assessed). '
  'Additive: a period rate is SUM(flagged)/SUM(assessed), never an average of rates. '
  'Not messages-derived — survives retention.';

CREATE INDEX IF NOT EXISTS idx_tone_daily_day ON manager_tone_daily (day);

CREATE TABLE IF NOT EXISTS manager_tone_flags (
    id              UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    manager_id      UUID NOT NULL REFERENCES internal_users(id),
    chat_id         UUID NOT NULL REFERENCES chats(id),
    -- CASCADE on purpose: purge_old_data() deletes messages older than the
    -- retention window and must not trip over this FK. The counter row above is
    -- what the page needs long-term; the flag is drill-down while the message lives.
    message_id      UUID NOT NULL REFERENCES messages(id) ON DELETE CASCADE,
    metric          TEXT NOT NULL,
    confidence      REAL NOT NULL,
    -- Verbatim excerpt; the acceptance gate rejected anything it could not find
    -- in the message text, so this is always a real substring.
    quote           TEXT NOT NULL,
    reason          TEXT NOT NULL,
    occurred_at     TIMESTAMPTZ NOT NULL,
    day             DATE NOT NULL,
    sender_name     TEXT,
    model           TEXT NOT NULL,
    prompt_version  TEXT NOT NULL,
    created_at      TIMESTAMPTZ NOT NULL DEFAULT now(),
    -- One flag per (message, metric): a resumed or repeated pass cannot double up.
    UNIQUE (message_id, metric)
);

COMMENT ON TABLE manager_tone_flags IS
  'Accepted tone-of-voice flags with verbatim quotes, for the dossier review list. '
  'Separate from risk_events by design — never alerts.';

CREATE INDEX IF NOT EXISTS idx_tone_flags_manager_day
    ON manager_tone_flags (manager_id, day DESC);
CREATE INDEX IF NOT EXISTS idx_tone_flags_day
    ON manager_tone_flags (day DESC, occurred_at DESC);

CREATE TABLE IF NOT EXISTS manager_tone_progress (
    chat_id        UUID NOT NULL REFERENCES chats(id),
    day            DATE NOT NULL,
    -- Messages rendered (context excluded) and manager messages judged.
    messages       INT NOT NULL DEFAULT 0,
    assessed       INT NOT NULL DEFAULT 0,
    flags          INT NOT NULL DEFAULT 0,
    input_tokens   INT NOT NULL DEFAULT 0,
    output_tokens  INT NOT NULL DEFAULT 0,
    cost_usd       NUMERIC(10, 6) NOT NULL DEFAULT 0,
    model          TEXT NOT NULL,
    prompt_version TEXT NOT NULL,
    created_at     TIMESTAMPTZ NOT NULL DEFAULT now(),
    PRIMARY KEY (chat_id, day)
);

COMMENT ON TABLE manager_tone_progress IS
  'Chat-days completed by the daily tone pass — idempotency, resume, and per-day '
  'spend accounting (the TONE_DAILY_BUDGET_USD ceiling reads SUM(cost_usd)).';

CREATE INDEX IF NOT EXISTS idx_tone_progress_created ON manager_tone_progress (created_at);

ALTER TABLE manager_tone_daily    ENABLE ROW LEVEL SECURITY;
ALTER TABLE manager_tone_flags    ENABLE ROW LEVEL SECURITY;
ALTER TABLE manager_tone_progress ENABLE ROW LEVEL SECURITY;

COMMIT;
