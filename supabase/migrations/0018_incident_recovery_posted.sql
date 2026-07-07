-- 0018: recovery alerts for payment incidents.
-- When a previously-announced incident returns to a resolved status the worker
-- broadcasts a one-shot "PSP recovered" alert into the same partner groups. This
-- flag makes that broadcast fire exactly once — later ticks still see the
-- incident as resolved but skip it because recovery_posted is already true.

ALTER TABLE payment_incidents
    ADD COLUMN IF NOT EXISTS recovery_posted BOOLEAN NOT NULL DEFAULT false;
