"""Entry point: setup, register handlers, start app. Phase 1/3.

FastAPI app that owns the DB pool lifecycle and the Telegram webhook
registration. Incoming updates are verified against the webhook secret and fed
into the aiogram dispatcher. The Slack-callback handler is a stub until Phase 12.
"""

from __future__ import annotations

import asyncio
import contextlib
import hmac
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from aiogram.types import Update
from fastapi import FastAPI, Request, Response

# Logging must be configured before anything else logs.
from src.utils.logging import get_logger, setup_logging

setup_logging()

from src.bot.instance import bot, dp  # noqa: E402  (after setup_logging on purpose)
from src.config import settings  # noqa: E402
from src.db.client import acquire_connection, close_pool, get_pool  # noqa: E402
from src.pipeline.tier1 import pattern_cache  # noqa: E402
from src.pipeline.workers import (  # noqa: E402
    abandoned_chat_cleanup_loop,
    pattern_reload_loop,
)

log = get_logger(__name__)

# Header Telegram sends with each webhook call, echoing the secret we registered.
_SECRET_HEADER = "X-Telegram-Bot-Api-Secret-Token"


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

    try:
        yield
    finally:
        # --- shutdown ---
        for task in (cleanup_task, pattern_task):
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
async def slack_callback(request: Request) -> dict[str, bool]:
    """Slack interactivity callback. Signature check + actions land in Phase 12."""
    return {"ok": True}


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
