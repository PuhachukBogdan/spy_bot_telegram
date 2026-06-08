"""Entry point: setup, register handlers, start app. Phase 1/3/16.

FastAPI app that owns the DB pool lifecycle and the Telegram webhook
registration. Incoming updates are verified against the webhook secret and fed
into the aiogram dispatcher.
"""

from __future__ import annotations

import asyncio
import contextlib
import hmac
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from datetime import UTC, datetime
from typing import Literal

from aiogram.types import Update
from fastapi import FastAPI, Request, Response

# Logging must be configured before anything else logs.
from src.utils.logging import get_logger, setup_logging

setup_logging()

from src.alerts.slack_callbacks import handle_slack_action, verify_slack_signature  # noqa: E402
from src.bot.instance import bot, dp  # noqa: E402  (after setup_logging on purpose)
from src.config import settings  # noqa: E402
from src.db.client import acquire_connection, close_pool, get_pool  # noqa: E402
from src.db.queries.summaries import get_rendered_html  # noqa: E402
from src.pipeline.tier1 import pattern_cache  # noqa: E402
from src.pipeline.workers import (  # noqa: E402
    abandoned_chat_cleanup_loop,
    analysis_worker_loop,
    file_analysis_worker_loop,
    pattern_reload_loop,
    whisper_worker_loop,
)
from src.summary.generator import generate_report  # noqa: E402

log = get_logger(__name__)

# Header Telegram sends with each webhook call, echoing the secret we registered.
_SECRET_HEADER = "X-Telegram-Bot-Api-Secret-Token"

# Telegram only sends the update types we explicitly ask for. The default set
# OMITS business_* updates (and chat_member / callback_query), so without this
# list the Business secretary handlers and the inline admin panel would never
# fire. We must therefore also re-list every type we already rely on (message /
# edited_message / my_chat_member); leaving one out would silently stop it.
_ALLOWED_UPDATES = [
    "message",
    "edited_message",
    "my_chat_member",
    "chat_member",
    "callback_query",
    "business_connection",
    "business_message",
    "edited_business_message",
    "deleted_business_messages",
]


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncIterator[None]:
    """Open the shared DB pool and register the Telegram webhook on startup;
    tear both down on shutdown."""
    # --- DB pool (single process-wide pool from src.db.client) ---
    log.info("startup.db.connect")
    await get_pool()  # warms the pool and fails fast if the DB is unreachable
    log.info("startup.db.connected")

    # --- Tier-1 dictionary: load before serving so the first message matches ---
    async with acquire_connection() as conn:
        await pattern_cache.refresh(conn)
    log.info("startup.patterns.loaded", count=pattern_cache.size)

    # --- Telegram webhook ---
    try:
        await bot.set_webhook(
            url=settings.TELEGRAM_WEBHOOK_URL,
            secret_token=settings.TELEGRAM_WEBHOOK_SECRET.get_secret_value(),
            allowed_updates=_ALLOWED_UPDATES,
            drop_pending_updates=False,
        )
        log.info("startup.webhook.set", url=settings.TELEGRAM_WEBHOOK_URL)
    except Exception as exc:  # don't let a transient Telegram error kill the app
        log.error("startup.webhook.failed", error=str(exc))

    # --- background workers ---
    cleanup_task = asyncio.create_task(
        abandoned_chat_cleanup_loop(bot), name="abandoned_chat_cleanup"
    )
    pattern_task = asyncio.create_task(
        pattern_reload_loop(), name="pattern_reload"
    )
    whisper_task = asyncio.create_task(
        whisper_worker_loop(bot), name="whisper_worker"
    )
    analysis_task = asyncio.create_task(
        analysis_worker_loop(bot), name="analysis_worker"
    )
    file_task = asyncio.create_task(
        file_analysis_worker_loop(bot), name="file_analysis_worker"
    )
    log.info("startup.whisper.worker", enabled=settings.WHISPER_ENABLED)
    log.info("startup.file_analysis.worker", enabled=settings.FILE_ANALYSIS_ENABLED)

    try:
        yield
    finally:
        # --- shutdown ---
        for task in (cleanup_task, pattern_task, whisper_task, analysis_task, file_task):
            task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await task

        try:
            await bot.delete_webhook(drop_pending_updates=False)
            log.info("shutdown.webhook.deleted")
        except Exception as exc:
            log.error("shutdown.webhook.failed", error=str(exc))

        await bot.session.close()
        await close_pool()


app = FastAPI(title="TG Partner Chat Risk Monitor", lifespan=lifespan)


@app.get("/health")
async def health() -> Response:
    """Liveness + DB readiness. Returns 503 if the pool can't answer SELECT 1."""
    try:
        async with acquire_connection() as conn:
            await conn.execute("SELECT 1")
    except Exception as exc:
        log.error("health.db.unreachable", error=str(exc))
        return _json({"status": "degraded", "db": "down"}, status_code=503)
    return _json({"status": "ok", "db": "up"})


@app.post("/webhook")
async def webhook(request: Request) -> Response:
    """Telegram webhook: verify the secret token, then dispatch the update.

    Verification compares the ``X-Telegram-Bot-Api-Secret-Token`` header against
    our configured secret with a constant-time check; a mismatch is rejected with
    401 and never dispatched (CLAUDE.md 7.1 step 2).

    Phase 3 handlers (DM commands) are fast, so the update is dispatched inline
    and we still return promptly. The heavy work added in Phase 5+ (LLM calls,
    transcription) must go onto ``processing_queue`` rather than block here, to
    stay under Telegram's ~10s webhook timeout (CLAUDE.md 11.2).
    """
    provided = request.headers.get(_SECRET_HEADER, "")
    expected = settings.TELEGRAM_WEBHOOK_SECRET.get_secret_value()
    if not hmac.compare_digest(provided, expected):
        log.warning("webhook.bad_secret")
        return _json({"ok": False}, status_code=401)

    try:
        payload = await request.json()
        update = Update.model_validate(payload)
    except Exception as exc:
        # Malformed body: ack with 200 so Telegram does not retry a bad update.
        log.error("webhook.bad_payload", error=str(exc))
        return _json({"ok": True})

    try:
        await dp.feed_update(bot, update)
    except Exception as exc:
        # Never surface a 500 to Telegram (it would retry for up to 24h).
        log.error("webhook.dispatch_failed", update_id=update.update_id, error=str(exc))

    return _json({"ok": True})


@app.post("/slack/callback")
async def slack_callback(request: Request) -> Response:
    """Slack interactivity callback: verify signature, dispatch action.

    Slack expects a 200 within 3 seconds. Signature verification is synchronous;
    action dispatch (DB + message update) runs inline — all async operations are
    fast enough to complete well within the timeout.
    """
    raw_body = await request.body()
    if not verify_slack_signature(request.headers, raw_body):
        log.warning("slack_callback.bad_signature")
        return Response(status_code=401)
    await handle_slack_action(raw_body)
    return Response(status_code=200)


@app.post("/summary/generate")
async def summary_generate(
    request: Request,
    period: Literal["weekly", "monthly"] = "weekly",
    token: str = "",
) -> Response:
    """Trigger HTML report generation for the given period.

    Called by n8n cron: POST /summary/generate?period=weekly&token=SECRET
    Returns JSON {ok, url} on success; 401 on bad token.
    """
    expected = settings.SUMMARY_ACCESS_TOKEN.get_secret_value()
    if not token or not hmac.compare_digest(token, expected):
        log.warning("summary_generate.bad_token", remote=request.client)
        return Response(status_code=401)
    url = await generate_report(period_type=period)
    return _json({"ok": True, "url": url})


@app.get("/reports/{period_type}/{date}")
async def get_report(
    period_type: str,
    date: str,
    token: str = "",
) -> Response:
    """Serve a pre-rendered HTML report.

    URL shape: GET /reports/weekly/2026-06-02?token=SECRET
    Returns 401 on bad token, 400 on bad date, 404 if no report exists yet,
    200 text/html otherwise.
    """
    expected = settings.SUMMARY_ACCESS_TOKEN.get_secret_value()
    if not token or not hmac.compare_digest(token, expected):
        return Response(status_code=401)
    try:
        period_start = datetime.strptime(date, "%Y-%m-%d").replace(tzinfo=UTC)
    except ValueError:
        return Response(status_code=400)
    async with acquire_connection() as conn:
        html = await get_rendered_html(conn, period_type, period_start)
    if html is None:
        return Response(status_code=404)
    return Response(content=html, media_type="text/html")


def _json(payload: dict[str, object], status_code: int = 200) -> Response:
    """Tiny JSONResponse helper (avoids importing JSONResponse at module top)."""
    from fastapi.responses import JSONResponse

    return JSONResponse(content=payload, status_code=status_code)


def main() -> None:
    """Console-script entry point (``tg-bot``). Runs the ASGI server."""
    import uvicorn

    uvicorn.run("src.main:app", host="0.0.0.0", port=8080, log_config=None)


if __name__ == "__main__":
    main()
