-- 0013_summary_share_token.sql
-- Capability-URL access for HTML reports (Google Drive-style).
-- Each report gets a unique random token; the public endpoint /r/{token} uses
-- only this token — no date/type in the URL, no global shared secret.
ALTER TABLE summaries
    ADD COLUMN IF NOT EXISTS share_token TEXT UNIQUE,   -- 64-char hex; NULL on old rows
    ADD COLUMN IF NOT EXISTS expires_at  TIMESTAMPTZ;   -- NULL = no expiry

CREATE INDEX IF NOT EXISTS idx_summaries_share_token ON summaries (share_token)
    WHERE share_token IS NOT NULL;
