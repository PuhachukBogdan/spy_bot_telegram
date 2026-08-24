-- 0024_archive_retro_findings.sql
-- Storage for the ONE-OFF retrospective risk analysis of imported archive history.
--
-- Deliberately NOT `risk_events`. The weekly/monthly reports select risk events by
-- `created_at` inside the period window; a retro pass run today would therefore
-- inject 2025-era findings into this week's report for every chat the archive
-- matched, and the Slack dashboard would present them as current risk. Retro
-- findings are also produced under a different, much stricter contract than the
-- live Tier-2 one, so mixing them would make `risk_events` mean two things at once.
--
-- The alert path is untouched by construction: `dispatch_alerts` reads
-- `risk_events`, so nothing here can page anyone.

BEGIN;

CREATE TABLE IF NOT EXISTS archive_retro_findings (
    id              UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    -- The imported message the finding is anchored on, and its chat. Both FK to
    -- the real tables so the report can join through to titles and text.
    message_id      UUID REFERENCES messages(id),
    chat_id         UUID REFERENCES chats(id),
    aff_id          TEXT,

    risk_type       TEXT NOT NULL,
    -- 0–100, as returned by the model. There is no Tier-1 base score and no
    -- confidence multiplier here: the retro contract is pass/fail on an explicit
    -- concealment marker, not the live `llm_score × multiplier` scoring.
    score           INT  NOT NULL,
    confidence      REAL NOT NULL,

    -- The exact span the model anchored on. Required: a finding that cannot quote
    -- its own evidence is precisely what this pass is built to exclude.
    quote           TEXT NOT NULL,
    -- Which concealment/bypass marker fired (the strict gate).
    marker          TEXT NOT NULL,
    explanation     TEXT NOT NULL,

    -- Other imported messages the model read as part of the same episode.
    context_message_ids UUID[],

    sender_name     TEXT,
    sender_role     TEXT,
    occurred_at     TIMESTAMPTZ NOT NULL,

    -- Provenance of the pass itself, so a re-run with a different model or prompt
    -- is distinguishable rather than silently mixed in.
    run_id          UUID NOT NULL,
    model           TEXT NOT NULL,
    prompt_version  TEXT NOT NULL,

    -- Human review, same vocabulary as risk_events.status.
    status          TEXT NOT NULL DEFAULT 'new',
    created_at      TIMESTAMPTZ NOT NULL DEFAULT now()
);

COMMENT ON TABLE archive_retro_findings IS
  'One-off retrospective risk findings over imported archive history. Separate from '
  'risk_events so they never enter live reports, alerts, or the Slack dashboard.';

CREATE INDEX IF NOT EXISTS idx_retro_findings_run
    ON archive_retro_findings (run_id, score DESC);
CREATE INDEX IF NOT EXISTS idx_retro_findings_chat
    ON archive_retro_findings (chat_id, occurred_at);
CREATE INDEX IF NOT EXISTS idx_retro_findings_type
    ON archive_retro_findings (risk_type, score DESC);

-- One finding per (run, message, risk_type): makes a resumed run idempotent.
CREATE UNIQUE INDEX IF NOT EXISTS idx_retro_findings_unique
    ON archive_retro_findings (run_id, message_id, risk_type);

-- Windows already sent to the model, so an interrupted run resumes instead of
-- re-paying for work it already did.
CREATE TABLE IF NOT EXISTS archive_retro_progress (
    run_id       UUID NOT NULL,
    chat_id      UUID NOT NULL REFERENCES chats(id),
    window_index INT  NOT NULL,
    -- Messages this window could anchor a finding on (excludes the lead-in overlap,
    -- which is read as context but belongs to the previous window). Recorded so the
    -- report states how much was actually analysed rather than inferring it from the
    -- chat's total — an interrupted run would otherwise overstate its own coverage.
    messages     INT NOT NULL DEFAULT 0,
    -- Cost accounting per window, so a partial run still reports what it spent.
    input_tokens  INT NOT NULL DEFAULT 0,
    output_tokens INT NOT NULL DEFAULT 0,
    cost_usd     NUMERIC(10, 6) NOT NULL DEFAULT 0,
    findings     INT NOT NULL DEFAULT 0,
    created_at   TIMESTAMPTZ NOT NULL DEFAULT now(),
    PRIMARY KEY (run_id, chat_id, window_index)
);

COMMENT ON TABLE archive_retro_progress IS
  'Windows completed per retro run — makes an interrupted pass resumable and keeps '
  'per-window token/cost accounting.';

ALTER TABLE archive_retro_findings  ENABLE ROW LEVEL SECURITY;
ALTER TABLE archive_retro_progress  ENABLE ROW LEVEL SECURITY;

COMMIT;
