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
| Deploy | Railway (auto-deploy on `git push` to `main`); Docker + Caddy for self-host |

---

## Project layout

See `CLAUDE.md` section 4 for the full annotated tree. Top level:

```
src/            application code (bot, db, pipeline, llm, alerts, summary, utils)
supabase/       SQL migrations (0001–0020, applied live)
prompts/        fallback LLM prompts (DB-backed at runtime)
tests/          unit + integration tests (no real DB/Slack/network)
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

## Deployment

**Production runs on Railway.** The `tg_ai_bot_bow` service is connected to this repo and
auto-deploys on every push to `main` — deploying is just `git push origin main` (never
`railway up`, which uploads the local dir instead of the git SHA). `/health` gates readiness;
migrations are applied to the live Supabase DB out of band. The Docker + Caddy recipe below is
the self-host alternative.

### Self-host (Docker + Caddy)

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

Phases 1–16 are complete and running in production on Railway: message ingestion, Tier-1
dictionary + Tier-2 LLM risk analysis, Slack alerts with interactive buttons and one-case/one-card
dedup, file (document) analysis, the manager-centric weekly/monthly HTML reports, the cost circuit
breaker, and the Ops Alerts subsystem. Phase 17 (load testing + pilot rollout) is in progress.
~308 tests, no real DB/Slack/network. See `CLAUDE.md` for the full annotated state and plan.
