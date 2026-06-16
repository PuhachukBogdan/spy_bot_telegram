-- Migration 0015: add slack_user_id to internal_users for /register OTP flow.
ALTER TABLE internal_users
    ADD COLUMN IF NOT EXISTS slack_user_id TEXT UNIQUE;
