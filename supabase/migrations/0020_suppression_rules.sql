-- 0020: staff-managed alert suppression rules (self-service false-positive gate).
-- A narrow, deterministic allowlist: an alertable risk_event whose detected_phrase
-- contains a rule's pattern (optionally scoped to a risk_type) is NOT posted to
-- Slack. The risk_event is still persisted (audit + report intact) — only the
-- alert is suppressed. Rules are created by the "🔕 Suppress" button on a card.
-- Deliberately narrow (matched against the specific phrase/excerpt) so a rule can
-- never silence a whole category and hide a genuinely new risk.

CREATE TABLE IF NOT EXISTS suppression_rules (
    id         UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    risk_type  TEXT,                                 -- NULL = any risk type
    pattern    TEXT NOT NULL,                        -- substring matched vs detected_phrase (normalized)
    note       TEXT,
    created_by TEXT,                                 -- Slack user who suppressed
    active     BOOLEAN NOT NULL DEFAULT true,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now()
);

-- RLS on, no policies (service_role BYPASSRLS reaches it; matches 0004/0016).
ALTER TABLE suppression_rules ENABLE ROW LEVEL SECURITY;
