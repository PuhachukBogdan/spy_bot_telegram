-- 0026: the `head` role — a department lead who reads the Team summary.
--
-- Why a fourth role rather than a flag: the dashboard now decides what a viewer
-- may see from `internal_users.role`, and the three existing values already carry
-- meanings the page must not inherit. `admin` sees everything including the risk
-- report; `manager` and `viewer` see no monitoring surface at all (the bot's
-- cover). `head` is the new middle: every manager's numbers EXCEPT their own.
--
-- The exclusion is the point. A head is also a working manager — they write in
-- partner chats, so they have SLA, tone and risk of their own — and a page that
-- showed a person their own record would turn a team tool into a mirror. The
-- page therefore drops the viewer from the roster, from the team sums and from
-- every risk case they authored (see src/metrics/scope.py).
--
-- Nobody is assigned the role here: it is granted case by case with /set_role,
-- which audits. Idempotent; safe to re-run.

BEGIN;

-- 1. Widen the role constraint. Dropping and re-adding is the only way to change
--    a CHECK; the transaction makes the gap invisible to other sessions.
ALTER TABLE internal_users
    DROP CONSTRAINT IF EXISTS internal_users_role_check;

ALTER TABLE internal_users
    ADD CONSTRAINT internal_users_role_check
    CHECK (role IN ('admin', 'manager', 'viewer', 'head'));

-- 2. The role index from 0007 (partial, enabled only) already serves
--    "find every head", which alert dispatch asks on the hot path.
CREATE INDEX IF NOT EXISTS idx_internal_users_role
    ON internal_users(role) WHERE enabled = true;

COMMIT;
