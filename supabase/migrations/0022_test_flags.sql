-- 0022_test_flags.sql
-- Mark test chats / test managers so the report can hide them by default
-- (toggleable in the report's filter panel). Additive, backward-compatible.

ALTER TABLE chats           ADD COLUMN IF NOT EXISTS is_test boolean NOT NULL DEFAULT false;
ALTER TABLE internal_users  ADD COLUMN IF NOT EXISTS is_test boolean NOT NULL DEFAULT false;

-- Seed the known test entities (the two earliest test chats + the test manager).
UPDATE chats
   SET is_test = true
 WHERE chat_name IN ('Test bot group 123', 'Vlad W arc');

UPDATE internal_users
   SET is_test = true
 WHERE full_name = 'Test#001';
