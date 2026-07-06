-- 0017: track the Slack message that advertises each dashboard link.
-- Lets a new report retire the previous link: the old dashboard token is
-- expired (revoked) and its Slack message is edited to drop the button, so only
-- the newest link stays active. The report content itself is unaffected — the
-- dashboard always renders the latest weekly + monthly.

ALTER TABLE dashboards
    ADD COLUMN IF NOT EXISTS slack_channel TEXT,
    ADD COLUMN IF NOT EXISTS slack_ts      TEXT;
