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

- Format: `.html` file posted to Slack (SLACK_CHANNEL_WEEKLY / SLACK_CHANNEL_MONTHLY)
- Trigger: n8n cron (weekly Monday 08:00; monthly 1st of month 08:00) — see ../n8n-workflows/
- LLM narrative (optional per-manager paragraph): model = LLM_MODEL_SUMMARY (claude-sonnet)

## This module (src/summary/)

- `filter.py` — noise filter: strips low-signal events before the summary builder
- Summary HTML builder lives in n8n (Phase 16) or a future `src/summary/builder.py`
- `summaries` and `summaries_skipped` DB tables already exist (migration 0001)
