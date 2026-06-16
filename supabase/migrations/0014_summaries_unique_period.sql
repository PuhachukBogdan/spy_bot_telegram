-- 0014_summaries_unique_period.sql
-- Prevent duplicate reports for the same period.
-- ON CONFLICT in save_summary() will UPDATE the existing row instead of inserting.
ALTER TABLE summaries
    ADD CONSTRAINT summaries_period_unique UNIQUE (period_type, period_start);
