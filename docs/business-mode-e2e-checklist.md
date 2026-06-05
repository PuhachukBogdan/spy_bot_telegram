# Telegram Business mode — e2e verification checklist

Read-only "secretary" mode. The bot is attached to an **account manager's**
Telegram Business account and silently monitors that manager's DMs with partners.
**Hard invariant under test: the bot only READS — it must never send a message
through a business connection.**

Source: `src/bot/handlers/business.py`. Automated coverage: `tests/test_business.py`
(31 tests, run `python -m pytest -q`). This document is the *live* pass the
automated tests cannot do — it needs real Telegram accounts and a running webhook.

---

## 0. Prerequisites

- [ ] Bot is deployed and reachable: uvicorn on `:8080` + Dev Tunnel **Public**, and
      `TELEGRAM_WEBHOOK_URL` points at the current tunnel URL (re-set on startup).
- [ ] **`business_connection`, `business_message`, `edited_business_message`,
      `deleted_business_messages` are in the webhook's `allowed_updates`.**
      Verify: `python scripts/webhook.py info` → the `allowed_updates` list contains
      all four `business_*` names (and `callback_query`). If they are missing, every
      business update is silently dropped — this is the #1 failure mode.
- [ ] Three distinct Telegram accounts/devices available:
  - **OWNER** — the account manager whose Business account the bot connects to.
  - **PARTNER** — a third account that DMs the OWNER (plays the monitored partner).
  - the **BOT** itself (its own account — never used as OWNER/PARTNER).
- [ ] OWNER has Telegram **Premium** (Business mode requires it).
- [ ] DB access (Supabase SQL editor or `psql` on `SUPABASE_DB_URL`) for the
      verification queries below.
- [ ] Note the numeric Telegram **user_id** of OWNER and PARTNER (e.g. via the bot's
      cover `/whoami`, or @userinfobot).

> Throughout: replace `<OWNER_ID>`, `<PARTNER_ID>`, `<BC_ID>` with real values.
> `<BC_ID>` is the `business_connection_id` string Telegram assigns on connect;
> read it from `/business_connections` or the `business_connections` table.

---

## 1. Connection grant — internal admin → auto-active

**Setup:** ensure OWNER is an enabled `internal_users` row with `role='admin'`.

1. [ ] On OWNER: Settings → Business → Chatbots → add this bot, grant **"read
       messages"** (and manage, if offered).
2. [ ] A `business_connection` update arrives.

**Expected**
- [ ] No DM is sent to anyone (admins are NOT pestered for an admin's own grant).
- [ ] DB row created `active`:
```sql
SELECT business_connection_id, status, internal_user_id, approved_by, approved_at
FROM business_connections WHERE business_account_user_id = <OWNER_ID>;
-- status = 'active', internal_user_id = OWNER's internal id, approved_by set.
```
- [ ] Audit row: `SELECT action FROM admin_audit_log WHERE action='business_connection_created' ORDER BY created_at DESC LIMIT 1;`

---

## 2. Connection grant — internal non-admin / external → pending + approve

**Setup:** make OWNER a `role='manager'` (or use a separate manager account), OR test
with an account that is not an internal user at all (external).

1. [ ] Connect the bot from that account (same Business → Chatbots flow).

**Expected**
- [ ] Grant lands `pending`:
```sql
SELECT status, internal_user_id FROM business_connections
WHERE business_connection_id = '<BC_ID>';   -- status = 'pending'
```
- [ ] **Every admin** receives a DM:
  - internal non-admin → "🔗 Business connection pending … role=manager"
  - external → "⚠️ EXTERNAL business connection … not an internal user"
  - both include copyable `/approve_business <BC_ID>` and `/reject_business <BC_ID>`.
- [ ] Send a partner message now (section 3) — it is **dropped** while pending
      (nothing in `messages`).
- [ ] Admin runs `/approve_business <BC_ID>` → reply "✅ approved"; row → `active`.
      (Or `/reject_business <BC_ID>` → "🚫 disabled"; row → `disabled`, messages stay dropped.)
- [ ] `/business_connections` (admin) lists the grant with its status + owner id.

---

## 3. Business message — unknown partner → pending unit + owner prompt

Connection must be `active` (sections 1/2).

1. [ ] From **PARTNER** (whose `user_id` is NOT yet in `partner_contacts`), send a DM
       to OWNER.

**Expected**
- [ ] A pending business unit is created (no partner bound):
```sql
SELECT telegram_chat_id, unit_type, status, partner_id, business_connection_id
FROM chats WHERE telegram_chat_id = <PARTNER_ID> AND unit_type = 'business';
-- status = 'pending', partner_id IS NULL, business_connection_id = '<BC_ID>'
```
- [ ] **Nothing is stored** from the message yet:
      `SELECT count(*) FROM messages WHERE chat_id = (SELECT id FROM chats WHERE telegram_chat_id=<PARTNER_ID> AND unit_type='business');` → **0**
- [ ] The **OWNER** (only) receives a DM: "🔗 New business contact … `/link_business_chat <BC_ID> <PARTNER_ID> "Partner Name"`".
- [ ] Send a 2nd message from PARTNER → **no second DM** (prompt is first-message only); still 0 stored.
- [ ] **PARTNER receives nothing from the bot** (read-only invariant — confirm on the PARTNER device).

---

## 4. Linking → monitoring starts

1. [ ] Admin runs `/link_business_chat <BC_ID> <PARTNER_ID> "Acme Corp"`.

**Expected**
- [ ] Reply "🔗 … linked to Acme Corp"; unit flips to `active` with a partner:
```sql
SELECT status, partner_id FROM chats
WHERE telegram_chat_id = <PARTNER_ID> AND unit_type = 'business';  -- status='active', partner_id set
```
2. [ ] From PARTNER, send a fresh DM to OWNER.
- [ ] It is now ingested with the business source:
```sql
SELECT source, business_connection_id, message_text, base_score, has_triggers
FROM messages
WHERE chat_id = (SELECT id FROM chats WHERE telegram_chat_id=<PARTNER_ID> AND unit_type='business')
ORDER BY created_at DESC LIMIT 1;   -- source = 'business', business_connection_id = '<BC_ID>'
```
- [ ] Send a message containing a known **red-flag pattern** (e.g. a Tier-1 dictionary
      phrase) → `has_triggers = true`, `base_score > 0`; if `base_score ≥
      PRIORITY_SCORE_THRESHOLD`, a `priority_llm` task is enqueued:
      `SELECT task_type, status FROM processing_queue WHERE task_type='priority_llm' ORDER BY created_at DESC LIMIT 1;`

---

## 5. Known contact → auto-link (no prompt)

**Setup:** pre-insert a contact for a *second* partner account `<PARTNER2_ID>`:
```sql
INSERT INTO partner_contacts (partner_id, telegram_user_id, full_name)
VALUES ('<EXISTING_PARTNER_UUID>', <PARTNER2_ID>, 'Known Partner');
```
1. [ ] From PARTNER2, DM the OWNER.

**Expected**
- [ ] Unit is created **active** immediately, bound to that partner — **no owner DM**:
```sql
SELECT status, partner_id FROM chats
WHERE telegram_chat_id = <PARTNER2_ID> AND unit_type = 'business';  -- 'active', partner_id matches
```
- [ ] The message is ingested (`source='business'`) on the first message.
- [ ] Audit: `action='business_chat_auto_linked'`.

---

## 6. Edit

1. [ ] PARTNER edits a previously-sent (and stored) message in the OWNER DM.

**Expected**
- [ ] A `message_edits` row is appended and the stored text is overwritten:
```sql
SELECT old_text, new_text FROM message_edits ORDER BY created_at DESC LIMIT 1;
```
- [ ] Tier-1 is re-run on the new text; if the edit pushes `base_score` across the
      threshold, a `priority_llm` task appears (as in section 4).
- [ ] Editing a message the bot never stored (e.g. one sent while pending) → **no
      `message_edits` row** (silently ignored).

---

## 7. Delete (soft-delete, never hard-delete)

1. [ ] PARTNER deletes one or more stored messages in the OWNER DM.

**Expected**
- [ ] Rows are **kept** but stamped (a deletion can itself be a risk signal):
```sql
SELECT telegram_message_id, deleted_at, deletion_payload IS NOT NULL AS has_payload
FROM messages
WHERE chat_id = (SELECT id FROM chats WHERE telegram_chat_id=<PARTNER_ID> AND unit_type='business')
  AND deleted_at IS NOT NULL ORDER BY deleted_at DESC;
```
- [ ] Audit: `action='business_messages_deleted'` with the `message_ids` + count.
- [ ] No escalation/alert fires (MVP just records).

---

## 8. Revoke

1. [ ] On OWNER: Settings → Business → Chatbots → remove the bot (or disable it).

**Expected**
- [ ] `business_connection` update with `is_enabled=false` → grant marked revoked:
```sql
SELECT status, revoked_at FROM business_connections
WHERE business_connection_id = '<BC_ID>';   -- status='revoked', revoked_at set
```
- [ ] Further partner messages on that connection are **dropped** (verify by sending
      one and confirming no new `messages` row and no new DM).

---

## 9. Read-only invariant — final sweep

Across **all** of the above, on the PARTNER device(s):
- [ ] The bot **never** sent a message, reaction, typing indicator, or read receipt
      that originated from us into the partner DM.
- [ ] The only bot-originated messages anywhere were **DMs to internal staff**
      (owner link prompts in §3, admin approval prompts in §2).

If any partner-facing send occurred, **stop** — that is a release blocker, and the
automated guard in `tests/conftest.py::FakeBot` should have caught it (re-run the
suite and add a regression test for the path that leaked).

---

## Sign-off

- [ ] Sections 1–9 pass on live Telegram.
- [ ] `python -m pytest -q` green (31 tests).
- [ ] `ruff check .` and `mypy src` clean.
- [ ] Record the run (date, tunnel URL, accounts used) in the session log.
