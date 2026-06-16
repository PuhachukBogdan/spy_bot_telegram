-- 0012_manager_identity.sql
-- Add aff_id (affiliate/partner-platform ID) and tg_username to internal_users.
-- Both are optional: existing rows keep NULL, display logic degrades gracefully.
ALTER TABLE internal_users
    ADD COLUMN IF NOT EXISTS aff_id     TEXT,
    ADD COLUMN IF NOT EXISTS tg_username TEXT;   -- stored WITHOUT leading @
