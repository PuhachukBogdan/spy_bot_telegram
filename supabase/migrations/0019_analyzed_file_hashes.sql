-- 0019: dedup identical documents so a report re-sent daily isn't re-analysed.
-- One row per (chat, content hash) that the file analyser has already processed.
-- On a new document the worker hashes the extracted text; if the (chat, hash) is
-- already present it skips before spending on the LLM. The first copy is always
-- analysed and alerts normally — only byte-identical re-sends are suppressed.

CREATE TABLE IF NOT EXISTS analyzed_file_hashes (
    id           UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    chat_id      UUID NOT NULL REFERENCES chats(id),
    content_hash TEXT NOT NULL,
    message_id   UUID REFERENCES messages(id),
    created_at   TIMESTAMPTZ NOT NULL DEFAULT now(),
    UNIQUE (chat_id, content_hash)
);

-- RLS on, no policies (service_role BYPASSRLS reaches it; matches 0004/0016).
ALTER TABLE analyzed_file_hashes ENABLE ROW LEVEL SECURITY;
