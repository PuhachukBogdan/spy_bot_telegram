# Summary generation — design notes

## Direction (locked 2026-06-08)

Weekly/monthly summaries are **manager-centric**, NOT per-partner flat lists.
Alerts (real-time Slack dispatch) are unaffected — Phase 11 format stays as-is.

## Report structure

**Page 1 — Manager × Category heat-map**
- Rows = managers (`internal_users WHERE role = 'manager'`)
- Columns = 12 risk categories (shadow_deal, private_channel, hidden_payment, …)
- Cells = count of `risk_events` in the period
- Attribution chain: `risk_events.partner_id → partners.owner_manager_id → internal_users`
- No denormalization — pure JOIN at report-build time

**Page 2+ — Per-manager timeline**
- One section per manager; chronological risk events across all their partners
- HIGH + CRITICAL visually highlighted (colour/bold)
- medium + low included (grey, smaller) for full portfolio picture

## Delivery

- Format: capability-URL link posted to Slack (SLACK_CHANNEL_REPORTS)
- Trigger: in-process scheduler `workers.summary_scheduler_loop`, all slots at **00:00 local time in `REPORT_TIMEZONE`** (Kyiv; = 21:00/22:00 UTC the day before, DST-dependent):
  - release (Slack post + new link): weekly Monday, monthly 1st of month → `generate_report`
  - content refresh (no Slack, no new link): every day → `refresh_report`, which stores a `'pending'` row that the dashboard picks up as the newest summary of its type
- The window ends at the scheduled slot, not at tick time, so consecutive reports are exactly contiguous. On-demand via POST /summary/generate (window ends now).
- LLM narrative (optional per-manager paragraph): model = LLM_MODEL_SUMMARY (claude-sonnet)

## This module (src/summary/)

- `filter.py` — noise filter: strips low-signal events before the summary builder
- `builder.py` — Jinja2 HTML render (light theme), `_manager_label`, RISK_CATEGORIES
- `generator.py` — orchestrator: DB queries → build → save (upsert) → Slack link
- `summaries` and `summaries_skipped` DB tables already exist (migration 0001)
