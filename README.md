# Telegram AI Agent — Partner Chat Risk Monitor

A bot that joins partner Telegram chats, **silently** reads all messages, detects risk signals
(shadow deals, moving to private channels, hidden payments, traffic leakage, conflicts), sends
alerts to Slack management, and produces weekly/monthly per-partner summaries.

> **Core rule:** the bot never writes in partner chats. All its communication happens in DMs with
> our staff and in Slack.

Full project context and the phased development plan live in [`CLAUDE.md`](./CLAUDE.md).

---

## Tech stack

| Component | Choice |
|---|---|
| Language | Python 3.11+ |
| Bot framework | aiogram 3.x |
| Web server | FastAPI + uvicorn (webhook, Slack callbacks, healthcheck) |
| Database | Supabase Pro (Postgres) via `asyncpg` |
| Validation | Pydantic v2 |
| HTTP client | httpx (async) |
| LLM | OpenRouter (Claude Haiku 4.5 / Sonnet 4.6) |
| Transcription | OpenAI Audio API (Whisper-1) |
| Slack | `slack_sdk` (Block Kit + Events API) |
| Queue | Postgres-as-queue (`FOR UPDATE SKIP LOCKED`) |
| Orchestration (summaries) | in-process asyncio scheduler (`workers.summary_scheduler_loop`) |
| Deploy | Docker + docker-compose, Caddy reverse proxy |

---

## Project layout

See `CLAUDE.md` section 4 for the full annotated tree. Top level:

```
src/            application code (bot, db, pipeline, llm, alerts, cost, utils)
supabase/       SQL migrations (17 tables)
prompts/        fallback LLM prompts (DB-backed at runtime)
tests/          unit + integration tests
```

---

## Local development

Requires Python 3.11+ and (for transcription) `ffmpeg` on PATH.

```bash
# 1. Create and fill the environment file
cp .env.example .env
#    generate the webhook secret:
python -c "import secrets; print(secrets.token_urlsafe(32))"

# 2. Install dependencies (editable, with dev extras)
pip install -e ".[dev]"

# 3. Run the app (FastAPI exposes /health, /webhook, /slack/callback)
uvicorn src.main:app --host 0.0.0.0 --port 8080 --reload

# 4. Sanity check
curl http://localhost:8080/health
```

---

## Deployment (Docker + Caddy)

1. **Prerequisites:** a Linux VM with a public IP and a DNS record pointing your domain
   (e.g. `bot.yourcompany.com`) at it. Ports 80 and 443 open.
2. **Configure:**
   - Fill in `.env` (see `.env.example`). Set `TELEGRAM_WEBHOOK_URL` to `https://<domain>/webhook`.
   - Edit `Caddyfile` and replace `bot.yourcompany.com` with your real domain.
   - Set `.env` file permissions to `600`.
3. **One-time bot setup with @BotFather:**
   - Create the bot, copy the token into `TELEGRAM_BOT_TOKEN`.
   - `/setprivacy → Disable` (so the bot receives all group messages).
4. **Run:**
   ```bash
   docker compose up -d --build
   docker compose ps        # both services healthy
   docker compose logs -f bot
   ```
   Caddy auto-provisions a Let's Encrypt certificate for your domain.
5. **Database:** apply migrations in `supabase/migrations/` via the Supabase CLI or dashboard.
6. **Seed data** (required before real operation — see `CLAUDE.md` section 12):
   - `internal_users`, `red_flag_patterns`, `critical_alert_recipients`, Slack channel IDs.

The webhook is registered automatically on startup via `bot.set_webhook()` using
`TELEGRAM_WEBHOOK_URL` and `TELEGRAM_WEBHOOK_SECRET`.

---

## Development status

Phase 1 (foundation) is scaffolded: project config, folder structure, Docker, Caddy. Module files
are stubs annotated with the phase that implements them. See `CLAUDE.md` section 8 for the full
phased plan.
