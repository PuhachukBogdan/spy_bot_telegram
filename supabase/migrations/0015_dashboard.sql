-- 0015: per-report access passwords + shared dashboard view
-- Applied: 2026-06-24

ALTER TABLE summaries
    ADD COLUMN IF NOT EXISTS access_password TEXT;

CREATE TABLE IF NOT EXISTS dashboards (
    id              UUID        PRIMARY KEY DEFAULT gen_random_uuid(),
    share_token     TEXT        UNIQUE NOT NULL,
    access_password TEXT        NOT NULL,
    expires_at      TIMESTAMPTZ,
    created_at      TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE INDEX IF NOT EXISTS ix_dashboards_share_token ON dashboards (share_token);
CREATE INDEX IF NOT EXISTS ix_dashboards_expires_at  ON dashboards (expires_at);
