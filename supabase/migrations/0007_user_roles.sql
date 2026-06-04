-- =============================================================================
-- 0007_user_roles.sql
-- Role-based access control for internal users.
--
-- internal_users gains a `role` (admin / manager / viewer). The legacy boolean
-- `is_admin` column is NOT dropped (backward compatibility for anything still
-- reading it / for a safe rollback), but application code treats `role` as the
-- single source of truth — InternalUser.is_admin is now a computed property
-- (role == 'admin').
--
-- Numbered 0007, not 0004: 0004_enable_rls.sql / 0005_topic_units.sql /
-- 0006_topics_and_business.sql already exist and are applied live, so the "0004"
-- the original plan asked for would collide. Content is otherwise as specified.
--
-- Idempotent: ADD COLUMN IF NOT EXISTS, a guarded CHECK constraint (DO-block on
-- pg_constraint, like 0005), CREATE INDEX IF NOT EXISTS, and an UPDATE that is
-- safe to re-run. Backfill order: add column (defaults every existing row to
-- 'manager') -> promote is_admin rows to 'admin' -> only then add the CHECK.
-- =============================================================================

BEGIN;

-- 1. role column. NOT NULL DEFAULT 'manager' backfills existing rows to manager.
ALTER TABLE internal_users
    ADD COLUMN IF NOT EXISTS role TEXT NOT NULL DEFAULT 'manager';

-- 2. Promote existing admins (legacy is_admin flag) before the CHECK is added.
UPDATE internal_users SET role = 'admin' WHERE is_admin = true AND role <> 'admin';

-- 3. Constrain to the three valid roles (guarded so re-runs are a no-op).
DO $$
BEGIN
    IF NOT EXISTS (
        SELECT 1 FROM pg_constraint WHERE conname = 'internal_users_role_check'
    ) THEN
        ALTER TABLE internal_users
            ADD CONSTRAINT internal_users_role_check
            CHECK (role IN ('admin', 'manager', 'viewer'));
    END IF;
END$$;

-- 4. Index for role-scoped lookups (e.g. "DM all enabled admins").
CREATE INDEX IF NOT EXISTS idx_internal_users_role
    ON internal_users(role) WHERE enabled = true;

COMMIT;
