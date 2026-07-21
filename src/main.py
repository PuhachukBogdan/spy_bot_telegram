"""Entry point: setup, register handlers, start app. Phase 1/3/16.

FastAPI app that owns the DB pool lifecycle and the Telegram webhook
registration. Incoming updates are verified against the webhook secret and fed
into the aiogram dispatcher.
"""

from __future__ import annotations

import asyncio
import contextlib
import hmac
import html as _html
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from datetime import UTC, datetime, timedelta
from typing import Literal

from aiogram.types import Update
from fastapi import FastAPI, Form, Request, Response
from fastapi.responses import RedirectResponse

# Logging must be configured before anything else logs.
from src.utils.logging import get_logger, setup_logging

setup_logging()

from src.alerts.slack_callbacks import handle_slack_action, verify_slack_signature  # noqa: E402
from src.bot.instance import bot, dp  # noqa: E402  (after setup_logging on purpose)
from src.config import settings  # noqa: E402
from src.db.client import acquire_connection, close_pool, get_pool  # noqa: E402
from src.db.queries.daily import (  # noqa: E402
    DIGEST_MAX_AGE_DAYS,
    get_daily_digest,
    resolve_digest_day,
)
from src.db.queries.summaries import (  # noqa: E402
    dashboard_token_known,
    get_dashboard_by_token,
    get_latest_summary_html,
    get_summary_by_share_token,
)
from src.pipeline.ops_alerts.scheduler import start_ops_alerts, stop_ops_alerts  # noqa: E402
from src.pipeline.tier1 import pattern_cache  # noqa: E402
from src.pipeline.workers import (  # noqa: E402
    abandoned_chat_cleanup_loop,
    analysis_worker_loop,
    failed_alert_retry_loop,
    file_analysis_worker_loop,
    pattern_reload_loop,
    stale_task_reaper_loop,
    storage_monitor_loop,
    summary_scheduler_loop,
    whisper_worker_loop,
)
from src.summary.builder import build_daily_card, build_dashboard_html  # noqa: E402
from src.summary.generator import generate_report  # noqa: E402

log = get_logger(__name__)

# Auth cookie lifetime — keeps the browser session alive across F5 reloads
# without re-prompting, but a fresh tab (no cookie) always requires the password.
_COOKIE_MAX_AGE = 86400  # 24 h


def _auth_cookie(prefix: str, token: str) -> str:
    """Stable cookie name scoped to this specific token."""
    return f"{prefix}_{token[:16]}"


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
    reaper_task = asyncio.create_task(
        stale_task_reaper_loop(), name="stale_task_reaper"
    )
    summary_task = asyncio.create_task(
        summary_scheduler_loop(), name="summary_scheduler"
    )
    failed_alert_task = asyncio.create_task(
        failed_alert_retry_loop(bot), name="failed_alert_retry"
    )
    storage_task = asyncio.create_task(
        storage_monitor_loop(bot), name="storage_monitor"
    )
    ops_alerts_tasks = start_ops_alerts(bot)
    log.info("startup.whisper.worker", enabled=settings.WHISPER_ENABLED)
    log.info("startup.file_analysis.worker", enabled=settings.FILE_ANALYSIS_ENABLED)
    log.info("startup.summary_scheduler.worker")
    log.info(
        "startup.storage_monitor.worker",
        limit_mb=settings.SUPABASE_DB_SIZE_LIMIT_MB,
        threshold_pct=settings.STORAGE_ALERT_THRESHOLD_PERCENT,
    )
    log.info("startup.ops_alerts.worker", enabled=settings.OPS_ALERTS_ENABLED)

    try:
        yield
    finally:
        # --- shutdown ---
        bg_tasks = (
            cleanup_task, pattern_task, whisper_task, analysis_task,
            file_task, reaper_task, summary_task, failed_alert_task,
            storage_task,
        )
        for task in bg_tasks:
            task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await task
        await stop_ops_alerts(ops_alerts_tasks)

        # Do NOT delete_webhook on shutdown: Railway rolling deploys start the
        # new container before stopping the old one, so delete_webhook from the
        # dying container would wipe the URL the new container just registered.
        # The new container re-registers on startup; Telegram queues any updates
        # delivered during the brief gap (drop_pending_updates=False on set_webhook).
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

    Manual trigger: POST /summary/generate?period=weekly&token=SECRET. The
    scheduled (weekly/monthly) runs are fired in-process by
    ``workers.summary_scheduler_loop`` — this endpoint is for on-demand reports.
    Returns JSON {ok, url, event_count, slack_delivered, slack_error} on success;
    401 on bad token.
    """
    expected = settings.SUMMARY_ACCESS_TOKEN.get_secret_value()
    if not token or not hmac.compare_digest(token, expected):
        log.warning("summary_generate.bad_token", remote=request.client)
        return Response(status_code=401)
    result = await generate_report(period_type=period)
    return _json(
        {
            "ok": True,
            "url": result.url,
            "event_count": result.event_count,
            "slack_delivered": result.slack_delivered,
            "slack_error": result.slack_error,
            "dashboard_password": result.dashboard_password,
        }
    )


def _pw_form(*, title: str, action: str, error: bool = False) -> str:
    """Minimal password gate page. All values are HTML-escaped."""
    t = _html.escape(title)
    a = _html.escape(action)
    err = '<p class="err">Incorrect password. Try again.</p>' if error else ""
    return (
        "<!DOCTYPE html>"
        '<html lang="en"><head><meta charset="utf-8">'
        '<meta name="viewport" content="width=device-width, initial-scale=1">'
        f"<title>{t}</title>"
        "<style>"
        "body{font-family:system-ui,sans-serif;background:#f4f5f7;"
        "display:flex;align-items:center;justify-content:center;height:100vh;margin:0}"
        ".card{background:#fff;border-radius:12px;padding:36px 40px;width:340px;"
        "box-shadow:0 1px 3px rgba(0,0,0,.1)}"
        "h1{font-size:18px;font-weight:700;margin-bottom:6px;color:#0f172a}"
        "p.sub{color:#94a3b8;font-size:13px;margin-bottom:22px}"
        "input[type=password]{width:100%;padding:10px 14px;border:1px solid #e2e8f0;"
        "border-radius:8px;font-size:14px;outline:none;box-sizing:border-box}"
        "input[type=password]:focus{border-color:#4f46e5}"
        "button{margin-top:12px;width:100%;padding:10px;background:#4f46e5;"
        "color:#fff;border:none;border-radius:8px;font-size:14px;font-weight:600;cursor:pointer}"
        "button:hover{background:#4338ca}"
        ".err{color:#b91c1c;font-size:12.5px;margin-top:10px}"
        "</style></head><body>"
        f'<div class="card"><h1>{t}</h1>'
        '<p class="sub">Enter the access password from Slack.</p>'
        f'<form method="post" action="{a}">'
        '<input type="password" name="pw" placeholder="Password" autofocus autocomplete="off">'
        "<button type=\"submit\">Open Report</button>"
        f"{err}"
        "</form></div></body></html>"
    )


def _superseded_page() -> str:
    """Shown when a dashboard token exists but was retired by a newer report."""
    return (
        "<!DOCTYPE html>"
        '<html lang="en"><head><meta charset="utf-8">'
        '<meta name="viewport" content="width=device-width, initial-scale=1">'
        "<title>Report superseded</title>"
        "<style>"
        "body{font-family:system-ui,sans-serif;background:#f4f5f7;"
        "display:flex;align-items:center;justify-content:center;height:100vh;margin:0}"
        ".card{background:#fff;border-radius:12px;padding:36px 40px;width:380px;"
        "text-align:center;box-shadow:0 1px 3px rgba(0,0,0,.1)}"
        "h1{font-size:18px;font-weight:700;margin-bottom:10px;color:#0f172a}"
        "p{color:#64748b;font-size:13.5px;line-height:1.6}"
        "</style></head><body>"
        '<div class="card"><h1>This report link has been replaced</h1>'
        "<p>A newer report has since been generated. Open the latest report "
        "message in your Slack reports channel to view the current dashboard.</p>"
        "</div></body></html>"
    )


@app.get("/r/{share_token}")
async def get_report(share_token: str, request: Request) -> Response:
    """Serve a pre-rendered HTML report by its capability token.

    Password-gated: POST /r/{token} verifies the password and sets an auth
    cookie; subsequent GET requests in the same browser session bypass the form.
    A fresh tab (no cookie) always shows the password prompt.
    """
    async with acquire_connection() as conn:
        row = await get_summary_by_share_token(conn, share_token)
    if row is None:
        return Response(status_code=404)
    access_pw: str | None = row.get("access_password")
    if access_pw:
        cookie = _auth_cookie("r", share_token)
        if request.cookies.get(cookie) != "1":
            return Response(
                content=_pw_form(title="Risk Report", action=f"/r/{share_token}"),
                media_type="text/html",
            )
    return Response(content=row["rendered_html"], media_type="text/html")


@app.post("/r/{share_token}")
async def post_report_auth(
    share_token: str, pw: str = Form("")
) -> Response:
    """Verify report password; on success set auth cookie and redirect to GET."""
    async with acquire_connection() as conn:
        row = await get_summary_by_share_token(conn, share_token)
    if row is None:
        return Response(status_code=404)
    access_pw: str | None = row.get("access_password")
    if not access_pw or not pw or not hmac.compare_digest(pw, access_pw):
        return Response(
            content=_pw_form(title="Risk Report", action=f"/r/{share_token}", error=True),
            media_type="text/html",
        )
    redirect = RedirectResponse(url=f"/r/{share_token}", status_code=303)
    redirect.set_cookie(
        key=_auth_cookie("r", share_token),
        value="1",
        httponly=True,
        secure=True,
        samesite="strict",
        max_age=_COOKIE_MAX_AGE,
    )
    return redirect


@app.get("/dashboard/{share_token}")
async def get_dashboard(share_token: str, request: Request) -> Response:
    """Serve the tabbed dashboard with the latest weekly and monthly reports.

    Password-gated: POST /dashboard/{token} verifies the password and sets an
    auth cookie; subsequent GET requests in the same browser session bypass the
    form. A fresh tab (no cookie) always shows the password prompt.
    """
    async with acquire_connection() as conn:
        dash = await get_dashboard_by_token(conn, share_token)
        if dash is None:
            # Distinguish a retired link (superseded by a newer report) from an
            # unknown one, so the user gets a helpful notice instead of a 404.
            known = await dashboard_token_known(conn, share_token)
        else:
            known = True
    if dash is None:
        if known:
            return Response(content=_superseded_page(), media_type="text/html")
        return Response(status_code=404)
    cookie = _auth_cookie("d", share_token)
    if request.cookies.get(cookie) != "1":
        return Response(
            content=_pw_form(
                title="Risk Reports Dashboard",
                action=f"/dashboard/{share_token}",
            ),
            media_type="text/html",
        )
    # Daily digest (first tab). Day from ?day= (default yesterday), clamped to 30d.
    today = datetime.now(UTC).date()
    day, _err = resolve_digest_day(request.query_params.get("day"), today)
    if day is None:
        day = today - timedelta(days=1)
    day_start = datetime(day.year, day.month, day.day, tzinfo=UTC)
    day_end = day_start + timedelta(days=1)
    async with acquire_connection() as conn:
        weekly_html = await get_latest_summary_html(conn, "weekly")
        monthly_html = await get_latest_summary_html(conn, "monthly")
        digest = await get_daily_digest(conn, day_start, day_end)
    daily_html = build_daily_card(
        day=day.isoformat(),
        digest=digest,
        min_day=(today - timedelta(days=DIGEST_MAX_AGE_DAYS)).isoformat(),
        max_day=today.isoformat(),
        generated_at=datetime.now(UTC).strftime("%Y-%m-%d %H:%M UTC"),
    )
    dashboard_html = build_dashboard_html(
        weekly_html=weekly_html, monthly_html=monthly_html, daily_html=daily_html
    )
    return Response(content=dashboard_html, media_type="text/html")


@app.post("/dashboard/{share_token}")
async def post_dashboard_auth(
    share_token: str, pw: str = Form("")
) -> Response:
    """Verify dashboard password; on success set auth cookie and redirect to GET."""
    async with acquire_connection() as conn:
        dash = await get_dashboard_by_token(conn, share_token)
    if dash is None:
        return Response(status_code=404)
    if not pw or not hmac.compare_digest(pw, str(dash["access_password"])):
        return Response(
            content=_pw_form(
                title="Risk Reports Dashboard",
                action=f"/dashboard/{share_token}",
                error=True,
            ),
            media_type="text/html",
        )
    redirect = RedirectResponse(url=f"/dashboard/{share_token}", status_code=303)
    redirect.set_cookie(
        key=_auth_cookie("d", share_token),
        value="1",
        httponly=True,
        secure=True,
        samesite="strict",
        max_age=_COOKIE_MAX_AGE,
    )
    return redirect


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
