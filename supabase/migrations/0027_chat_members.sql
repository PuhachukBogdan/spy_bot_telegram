-- 0027_chat_members.sql
-- Who is actually in which partner chat, and two person-level markers.
--
-- Until now the Team summary tied a chat to exactly ONE person: chats.authorized_by,
-- the manager who added the bot. Measured against Telegram on 2026-10-06 that model
-- was wrong for the whole team: a typical partner group has 3–5 of our managers in
-- it (244 of 317 active groups have four), the head of department sits in 270 of
-- them and the page showed him in 4, and a manager who left the company still
-- "owned" chats where colleagues were doing the work. Nothing recorded membership —
-- joins were logged only when Telegram happened to send a service message, and the
-- bot never asked.
--
-- chat_members is the answer: one row per (chat, Telegram account) for OUR accounts
-- (internal_users.telegram_accounts — partners are not tracked here), kept current
-- by three sources:
--   sweep    the membership worker asks getChatMember for every staff account in
--            every active group on a schedule (authoritative; also refreshes titles);
--   event    new_chat_members / left_chat_member / chat_member updates as they land;
--   message  a staff message in a chat is proof of presence (ingest touches the row).
-- `status` carries Telegram's own words (creator/administrator/member/restricted =
-- present; left/kicked = gone). History is kept: a row is never deleted when someone
-- leaves, so first_seen_at survives (it is what tells an OLD account from a NEW one).
--
-- Also here:
--   internal_users.deactivated_at / deactivation_note — a person who stopped working
--     but is deliberately NOT removed ("не уволен, просто ушёл — вдруг вернётся").
--     They stay on the dashboard with a badge and their history; from that date on
--     no chat or wait is attributed to them. Independent of `enabled` (bot access).
--   internal_users.account_labels — optional {telegram_id: 'old'|'new'} override for
--     the dashboard's per-account split; when empty the label is derived from which
--     account was seen first.
--
-- The backfill below seeds presence from the evidence already in the database
-- (who added the bot, recorded joins, staff messages). It is evidence of presence
-- only; the first sweep after deploy replaces it with Telegram's answer.
-- RLS on, no policies (service_role bypasses). Idempotent; safe to re-run.

BEGIN;

CREATE TABLE IF NOT EXISTS chat_members (
    chat_id          UUID        NOT NULL REFERENCES chats(id) ON DELETE CASCADE,
    telegram_user_id BIGINT      NOT NULL,
    -- Resolved at write time; NULL once the account is detached from every row.
    internal_user_id UUID        REFERENCES internal_users(id) ON DELETE SET NULL,
    -- Telegram's ChatMember status, or 'unknown' when it could not be asked.
    status           TEXT        NOT NULL,
    -- Earliest evidence of this account in this chat. Never moves forward.
    first_seen_at    TIMESTAMPTZ NOT NULL DEFAULT now(),
    -- Latest evidence of PRESENCE (message, join, or a sweep that found them in).
    last_seen_at     TIMESTAMPTZ NOT NULL DEFAULT now(),
    -- Last time getChatMember answered for this row (NULL = never verified).
    last_verified_at TIMESTAMPTZ,
    -- sweep / event / message / adder / backfill — what last wrote the status.
    source           TEXT        NOT NULL,
    updated_at       TIMESTAMPTZ NOT NULL DEFAULT now(),
    PRIMARY KEY (chat_id, telegram_user_id)
);

CREATE INDEX IF NOT EXISTS idx_chat_members_present
    ON chat_members (telegram_user_id, chat_id)
    WHERE status IN ('creator', 'administrator', 'member', 'restricted');
CREATE INDEX IF NOT EXISTS idx_chat_members_internal
    ON chat_members (internal_user_id);

ALTER TABLE chat_members ENABLE ROW LEVEL SECURITY;

ALTER TABLE internal_users
    ADD COLUMN IF NOT EXISTS deactivated_at    TIMESTAMPTZ,
    ADD COLUMN IF NOT EXISTS deactivation_note TEXT,
    ADD COLUMN IF NOT EXISTS account_labels    JSONB NOT NULL DEFAULT '{}'::jsonb;

-- ---------------------------------------------------------------------------
-- Backfill from existing evidence. Staff accounts = every id in telegram_accounts
-- of a real person (admin / head / manager, not a test row). Partners are not
-- tracked; stubs minted from an aff_id have no accounts and contribute nothing.
-- ---------------------------------------------------------------------------
WITH staff AS (
    SELECT u.id AS internal_user_id, (acc.value)::bigint AS telegram_user_id
    FROM internal_users u
    CROSS JOIN LATERAL jsonb_array_elements_text(COALESCE(u.telegram_accounts, '[]'::jsonb)) AS acc(value)
    WHERE u.role IN ('admin', 'head', 'manager')
      AND COALESCE(u.is_test, false) = false
      AND acc.value ~ '^[0-9]+$'
),
evidence AS (
    -- Whoever added the bot was in the chat when it was created.
    SELECT c.id AS chat_id, c.added_by_user_id AS telegram_user_id,
           c.created_at AS seen_at
    FROM chats c
    WHERE c.added_by_user_id IS NOT NULL
      AND c.status IN ('active', 'pending')
    UNION ALL
    -- Recorded joins (service messages / chat_member updates).
    SELECT e.chat_id, e.target_user_id, e.created_at
    FROM chat_events e
    WHERE e.event_type = 'member_join' AND e.target_user_id IS NOT NULL
    UNION ALL
    -- A staff message is proof of presence at that moment.
    SELECT m.chat_id, m.sender_id, m.timestamp
    FROM messages m
    WHERE m.sender_id IS NOT NULL AND m.source <> 'imported'
),
folded AS (
    SELECT e.chat_id, e.telegram_user_id, s.internal_user_id,
           MIN(e.seen_at) AS first_seen_at, MAX(e.seen_at) AS last_seen_at
    FROM evidence e
    JOIN staff s ON s.telegram_user_id = e.telegram_user_id
    JOIN chats c ON c.id = e.chat_id AND c.status IN ('active', 'pending')
    GROUP BY e.chat_id, e.telegram_user_id, s.internal_user_id
)
INSERT INTO chat_members (chat_id, telegram_user_id, internal_user_id, status,
                          first_seen_at, last_seen_at, source)
SELECT f.chat_id, f.telegram_user_id, f.internal_user_id, 'member',
       f.first_seen_at, f.last_seen_at, 'backfill'
FROM folded f
ON CONFLICT (chat_id, telegram_user_id) DO UPDATE
    SET first_seen_at = LEAST(chat_members.first_seen_at, EXCLUDED.first_seen_at),
        internal_user_id = COALESCE(chat_members.internal_user_id, EXCLUDED.internal_user_id);

COMMIT;
