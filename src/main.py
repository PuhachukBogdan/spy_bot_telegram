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
import re
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from datetime import datetime, timedelta
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
from src.db.models import InternalUser  # noqa: E402
from src.db.queries.daily import (  # noqa: E402
    DIGEST_MAX_AGE_DAYS,
    get_daily_digest,
    resolve_digest_day,
)
from src.db.queries.etc import (  # noqa: E402
    find_internal_user_by_telegram_id,
    get_internal_user_by_id,
)
from src.db.queries.summaries import (  # noqa: E402
    get_latest_summary_html,
    get_summary_by_share_token,
)
from src.importer.retro_report import (  # noqa: E402
    load_findings,
    load_latest_run_id,
    load_run_summary,
    render_report,
)
from src.metrics.cache import preview_cache  # noqa: E402
from src.metrics.preview import build_preview  # noqa: E402
from src.metrics.scope import DASHBOARD_ROLES, scope_for  # noqa: E402
from src.pipeline.ops_alerts.scheduler import start_ops_alerts, stop_ops_alerts  # noqa: E402
from src.pipeline.tier1 import pattern_cache  # noqa: E402
from src.pipeline.workers import (  # noqa: E402
    abandoned_chat_cleanup_loop,
    analysis_worker_loop,
    failed_alert_retry_loop,
    file_analysis_worker_loop,
    pattern_reload_loop,
    report_timezone,
    stale_task_reaper_loop,
    storage_monitor_loop,
    summary_scheduler_loop,
    tone_worker_loop,
    whisper_worker_loop,
)
from src.summary.builder import build_daily_card, build_dashboard_html  # noqa: E402
from src.summary.generator import generate_report  # noqa: E402
from src.utils.session import (  # noqa: E402
    SESSION_COOKIE,
    consume_login_token,
    sign_session,
    verify_session,
    verify_telegram_login,
)

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
        summary_scheduler_loop(bot), name="summary_scheduler"
    )
    failed_alert_task = asyncio.create_task(
        failed_alert_retry_loop(bot), name="failed_alert_retry"
    )
    storage_task = asyncio.create_task(
        storage_monitor_loop(bot), name="storage_monitor"
    )
    tone_task = asyncio.create_task(
        tone_worker_loop(bot), name="tone_worker"
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
    log.info(
        "startup.tone.worker",
        enabled=settings.TONE_ANALYSIS_ENABLED,
        model=settings.LLM_MODEL_TONE,
    )

    try:
        yield
    finally:
        # --- shutdown ---
        bg_tasks = (
            cleanup_task, pattern_task, whisper_task, analysis_task,
            file_task, reaper_task, summary_task, failed_alert_task,
            storage_task, tone_task,
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


async def _render_daily_panel(day_arg: str | None) -> str:
    """Render the Daily-digest panel body for ``?day=`` (default: the current day).

    Shared by the full dashboard and the hourly-refresh fragment route so the
    two can never drift. The digest is computed live from aggregate SQL (no LLM,
    nothing cached), so every call reflects the DB as of right now; an invalid
    or out-of-range ``day`` silently falls back to today.

    "Today" and the day boundaries are LOCAL to ``REPORT_TIMEZONE`` (Kyiv), the
    same zone the weekly/monthly slots fire in — so every date shown anywhere in
    the dashboard means the same calendar day. The window is still one 24h span,
    just aligned to local midnight instead of UTC midnight.
    """
    tz = report_timezone()
    now_local = datetime.now(tz)
    today = now_local.date()
    day, _err = resolve_digest_day(day_arg, today)
    if day is None:
        day = today
    day_start = datetime(day.year, day.month, day.day, tzinfo=tz)
    day_end = day_start + timedelta(days=1)
    async with acquire_connection() as conn:
        digest = await get_daily_digest(conn, day_start, day_end)
    return build_daily_card(
        day=day.isoformat(),
        digest=digest,
        min_day=(today - timedelta(days=DIGEST_MAX_AGE_DAYS)).isoformat(),
        max_day=today.isoformat(),
        generated_at=now_local.strftime(f"%Y-%m-%d %H:%M {now_local.tzname() or ''}").strip(),
    )


# --- dashboard: one URL, a signed-in viewer, a page scoped to their role ------
# Until 2026-09-11 the dashboard was a rotating capability URL plus one password
# shared in Slack. That design cannot answer "who is reading", and the `head`
# role is defined by the answer: a department lead sees every manager EXCEPT
# themselves. So the link is now fixed and carries no secret, and the viewer
# signs in as a person — by tapping a one-time link the bot DMs them, or through
# Telegram's Login Widget. Old tokenised URLs redirect here rather than 404, so
# links already sitting in Slack keep working.

_DASHBOARD_URL = "/dashboard"

#: Cached bot @username for the Login Widget; resolved once per process.
_bot_username: str | None = None


async def _login_widget_username() -> str | None:
    """The bot's @username, or ``None`` when Telegram cannot be reached.

    The widget is optional furniture: if this fails the login page still offers
    the bot-link route, which is the path most people take anyway.
    """
    global _bot_username
    if _bot_username is None:
        try:
            me = await bot.get_me()
        except Exception as exc:  # noqa: BLE001 — any failure means "no widget"
            log.warning("dashboard.bot_username_unavailable", error=str(exc))
            return None
        _bot_username = me.username
    return _bot_username


def _login_page(bot_username: str | None, *, error: str | None = None) -> str:
    """The sign-in page. Two ways in, both landing on the same session cookie."""
    widget = (
        '<script async src="https://telegram.org/js/telegram-widget.js?22" '
        f'data-telegram-login="{_html.escape(bot_username)}" data-size="large" '
        f'data-auth-url="{_html.escape(settings.SERVER_BASE_URL.rstrip("/"))}'
        '/auth/telegram" data-request-access="write"></script>'
        if bot_username
        else '<p class="muted">Telegram login is unavailable right now — '
        "use the bot link below.</p>"
    )
    bot_hint = (
        f"@{_html.escape(bot_username)}" if bot_username else "the monitoring bot"
    )
    err = f'<p class="err">{_html.escape(error)}</p>' if error else ""
    return (
        "<!DOCTYPE html>"
        '<html lang="en"><head><meta charset="utf-8">'
        '<meta name="viewport" content="width=device-width, initial-scale=1">'
        "<title>Team summary</title>"
        "<style>"
        "body{font-family:system-ui,sans-serif;background:#f4f5f7;"
        "display:flex;align-items:center;justify-content:center;min-height:100vh;margin:0}"
        ".card{background:#fff;border-radius:12px;padding:36px 40px;width:380px;"
        "box-shadow:0 1px 3px rgba(0,0,0,.1)}"
        "h1{font-size:18px;font-weight:700;margin:0 0 6px;color:#0f172a}"
        "p{color:#64748b;font-size:13.5px;line-height:1.6;margin:0 0 14px}"
        ".muted{color:#94a3b8;font-size:12.5px}"
        ".sep{border:0;border-top:1px solid #e2e8f0;margin:20px 0}"
        "code{background:#f1f5f9;padding:2px 6px;border-radius:5px;font-size:12.5px}"
        ".err{color:#b91c1c;font-size:12.5px}"
        "</style></head><body>"
        '<div class="card"><h1>Team summary</h1>'
        "<p>Sign in with the Telegram account you use at work.</p>"
        f"{widget}"
        '<hr class="sep">'
        f"<p>Or open a chat with {bot_hint} and send <code>/dashboard</code> — "
        "it replies with a link that signs you in on this device.</p>"
        f"{err}"
        "</div></body></html>"
    )


def _notice_page(title: str, body: str, *, status: int = 403) -> Response:
    """A plain message page — wrong account, expired link, mode not available."""
    html = (
        "<!DOCTYPE html>"
        '<html lang="en"><head><meta charset="utf-8">'
        '<meta name="viewport" content="width=device-width, initial-scale=1">'
        f"<title>{_html.escape(title)}</title>"
        "<style>"
        "body{font-family:system-ui,sans-serif;background:#f4f5f7;"
        "display:flex;align-items:center;justify-content:center;height:100vh;margin:0}"
        ".card{background:#fff;border-radius:12px;padding:36px 40px;width:380px;"
        "text-align:center;box-shadow:0 1px 3px rgba(0,0,0,.1)}"
        "h1{font-size:18px;font-weight:700;margin-bottom:10px;color:#0f172a}"
        "p{color:#64748b;font-size:13.5px;line-height:1.6}"
        "a{color:#4f46e5}"
        "</style></head><body>"
        f'<div class="card"><h1>{_html.escape(title)}</h1><p>{body}</p></div>'
        "</body></html>"
    )
    return Response(content=html, media_type="text/html", status_code=status)


async def _session_user(request: Request) -> InternalUser | None:
    """The signed-in viewer, or ``None``.

    The cookie only asserts an id: the role and the enabled flag are re-read from
    the database on every request, so ``/disable_user`` and ``/set_role`` take
    effect on the person's next page load instead of whenever a 90-day cookie
    happens to lapse.
    """
    claims = verify_session(request.cookies.get(SESSION_COOKIE))
    if claims is None:
        return None
    async with acquire_connection() as conn:
        user = await get_internal_user_by_id(conn, claims.user_id)
    if user is None or not user.enabled or user.role not in DASHBOARD_ROLES:
        return None
    return user


def _sign_in(user: InternalUser) -> RedirectResponse:
    """Set the session cookie and land on the dashboard."""
    redirect = RedirectResponse(url=_DASHBOARD_URL, status_code=303)
    redirect.set_cookie(
        key=SESSION_COOKIE,
        value=sign_session(user.id, user.role),
        httponly=True,
        secure=True,
        # Lax, not Strict: the sign-in arrives as a top-level navigation from
        # telegram.org (widget) or from the Telegram client (bot link), and
        # Strict would drop the cookie on exactly that first hop.
        samesite="lax",
        max_age=settings.DASHBOARD_SESSION_DAYS * 86400,
    )
    log.info("dashboard.signed_in", user=str(user.id)[:8], role=user.role)
    return redirect


@app.get("/dashboard")
async def get_dashboard(request: Request) -> Response:
    """The Team summary, scoped to whoever is signed in."""
    user = await _session_user(request)
    if user is None:
        return Response(
            content=_login_page(await _login_widget_username()),
            media_type="text/html",
        )
    return Response(
        content=await build_preview(
            fresh=request.query_params.get("fresh") == "1",
            include_tone=True,
            scope=scope_for(user),
        ),
        media_type="text/html",
    )


@app.get("/dashboard/risk")
async def get_dashboard_risk(request: Request) -> Response:
    """The classic risk report — weekly/monthly tabs + live daily digest.

    Admin only. It is rendered from stored HTML snapshots, which cannot be
    re-scoped per viewer, so there is no way to show a head everyone's cases but
    their own here — the Team summary's per-manager cards are their risk surface.
    """
    user = await _session_user(request)
    if user is None:
        return Response(
            content=_login_page(await _login_widget_username()),
            media_type="text/html",
        )
    if user.role != "admin":
        return _notice_page(
            "Not available",
            'The risk report is admin-only. <a href="/dashboard">Back to the '
            "Team summary</a>.",
        )
    day_arg = request.query_params.get("day")
    page = await _render_risk_report(
        day_arg,
        team_url=_DASHBOARD_URL,
        subtitle="weekly &middot; monthly &middot; daily",
        cache_key=f"risk:dash:{day_arg or 'today'}",
        fresh=request.query_params.get("fresh") == "1",
    )
    return Response(content=page, media_type="text/html")


@app.get("/dashboard/daily")
@app.get("/dashboard/risk/daily")
async def get_dashboard_daily(request: Request) -> Response:
    """Daily-digest panel fragment — polled once an hour by an open report.

    Returns the panel HTML only (no page shell), so the browser swaps it in
    place instead of reloading: the active tab and scroll position survive. A
    401 stops the polling, which is what should happen once a session lapses.
    """
    user = await _session_user(request)
    if user is None or user.role != "admin":
        return Response(status_code=401)
    return Response(
        content=await _render_daily_panel(request.query_params.get("day")),
        media_type="text/html",
        headers={"Cache-Control": "no-store"},
    )


@app.get("/auth/telegram")
async def auth_telegram(request: Request) -> Response:
    """Telegram Login Widget callback: verify the signature, start a session."""
    telegram_id = verify_telegram_login(dict(request.query_params))
    if telegram_id is None:
        return Response(
            content=_login_page(
                await _login_widget_username(),
                error="That sign-in could not be verified. Please try again.",
            ),
            media_type="text/html",
        )
    async with acquire_connection() as conn:
        user = await find_internal_user_by_telegram_id(conn, telegram_id)
    if user is None or user.role not in DASHBOARD_ROLES:
        log.info("dashboard.login_refused", telegram_id=telegram_id)
        return _notice_page(
            "No access",
            "This Telegram account is not set up to view the report. "
            "Ask an administrator to grant access.",
        )
    return _sign_in(user)


@app.get("/auth/link/{token}")
async def auth_link(token: str) -> Response:
    """One-time link from the bot: redeem it and start a session."""
    user_id = consume_login_token(token)
    if user_id is None:
        return _notice_page(
            "Link expired",
            "Sign-in links are valid for 15 minutes and can be used once. "
            "Send <b>/dashboard</b> to the bot for a fresh one.",
            status=410,
        )
    async with acquire_connection() as conn:
        user = await get_internal_user_by_id(conn, user_id)
    if user is None or not user.enabled or user.role not in DASHBOARD_ROLES:
        return _notice_page(
            "No access",
            "This account is not set up to view the report.",
        )
    return _sign_in(user)


@app.get("/logout")
async def logout() -> Response:
    """Drop the session cookie on this device."""
    redirect = RedirectResponse(url=_DASHBOARD_URL, status_code=303)
    redirect.delete_cookie(SESSION_COOKIE)
    return redirect


# Legacy tokenised URLs. Every one of them is in somebody's Slack history or
# browser history; redirecting costs two lines and saves a support question.
# Declared AFTER the fixed paths so "/dashboard/risk" is never read as a token.


@app.get("/dashboard/{share_token}")
async def get_dashboard_legacy(share_token: str) -> Response:
    return RedirectResponse(url=_DASHBOARD_URL, status_code=307)


@app.get("/dashboard/{share_token}/risk")
async def get_dashboard_risk_legacy(share_token: str) -> Response:
    return RedirectResponse(url="/dashboard/risk", status_code=307)


@app.get("/dashboard/{share_token}/daily")
@app.get("/dashboard/{share_token}/risk/daily")
async def get_dashboard_daily_legacy(share_token: str) -> Response:
    # A page left open on an old URL polls this; 404 stops it, and its next
    # reload lands on the redirect above.
    return Response(status_code=404)


# --- archive review: one permanent link, outside the report rotation -----------
# The weekly/monthly dashboard mints a new token on every generation and revokes the
# previous one, so its URL is deliberately short-lived. The archive review is the
# opposite: one link, fixed forever, that always renders the newest retro run. It
# therefore lives on its own route with its own credentials and touches none of the
# `summaries` / `dashboards` token machinery.


def _archive_credentials() -> tuple[str, str] | None:
    """``(token, password)`` if the permanent link is enabled, else ``None``."""
    token = settings.ARCHIVE_REPORT_TOKEN
    password = settings.ARCHIVE_REPORT_PASSWORD
    if token is None or password is None:
        return None
    token_value = token.get_secret_value()
    password_value = password.get_secret_value()
    if not token_value or not password_value:
        return None
    return token_value, password_value


async def _render_archive_review() -> str | None:
    """Render the newest retro run, or ``None`` when no run has completed."""
    async with acquire_connection() as conn:
        run_id = await load_latest_run_id(conn)
        if run_id is None:
            return None
        summary = await load_run_summary(conn, run_id)
        findings = await load_findings(conn, run_id)
    return render_report(summary, findings)


@app.get("/archive/{token}")
async def get_archive_review(token: str, request: Request) -> Response:
    """Serve the archive risk review on its permanent link.

    Rendered live from ``archive_retro_findings`` rather than served from a stored
    snapshot, so a finding a human later marks reviewed is reflected the next time
    the link is opened — a fixed URL that showed a frozen copy would drift away from
    the data it claims to report.
    """
    credentials = _archive_credentials()
    if credentials is None:
        return Response(status_code=404)
    expected_token, _ = credentials
    # Constant-time: the token is the only thing guarding a permanent URL.
    if not hmac.compare_digest(token, expected_token):
        return Response(status_code=404)

    if request.cookies.get(_auth_cookie("archive", token)) != "1":
        return Response(
            content=_pw_form(title="Archive Risk Review", action=f"/archive/{token}"),
            media_type="text/html",
        )

    html = await _render_archive_review()
    if html is None:
        return Response(
            content=_pw_form(title="Archive Risk Review — not yet generated",
                             action=f"/archive/{token}"),
            media_type="text/html",
            status_code=404,
        )
    return Response(content=html, media_type="text/html")


@app.post("/archive/{token}")
async def post_archive_auth(token: str, pw: str = Form("")) -> Response:
    """Verify the archive password; on success set the cookie and redirect to GET."""
    credentials = _archive_credentials()
    if credentials is None:
        return Response(status_code=404)
    expected_token, expected_pw = credentials
    if not hmac.compare_digest(token, expected_token):
        return Response(status_code=404)
    if not pw or not hmac.compare_digest(pw, expected_pw):
        return Response(
            content=_pw_form(
                title="Archive Risk Review", action=f"/archive/{token}", error=True
            ),
            media_type="text/html",
        )
    redirect = RedirectResponse(url=f"/archive/{token}", status_code=303)
    redirect.set_cookie(
        key=_auth_cookie("archive", token),
        value="1",
        httponly=True,
        secure=True,
        samesite="strict",
        max_age=_COOKIE_MAX_AGE,
    )
    return redirect


# --- the report shell's risk mode --------------------------------------------
# Retired 2026-09-11: the /preview stand (fixed token + password in .env) was a
# second, unscoped door to the admin view. The dashboard is now per-viewer, so
# the stand had nothing left to show that /dashboard does not.

def _stand_header(team_url: str, subtitle: str) -> str:
    """A header strip mirroring the Team summary's, injected above the tabs.

    Same skeleton on both pages — title top-left (display font, 27px), the mode
    switch top-right IN THE FLOW at the same offsets — so toggling modes moves
    nothing. The active segment is CRIT RED here and teal on the Team summary:
    the switch's colour says where you are without reading.

    ``team_url`` is where the Team summary segment points — the stand's
    ``/preview/{token}`` or the production ``/dashboard/{token}``.
    """
    return (
        '<div style="max-width:1180px;margin:0 auto;padding:30px 24px 0;display:flex;'
        'flex-wrap:wrap;justify-content:space-between;align-items:flex-start;gap:12px">'
        "<div>"
        "<div style=\"font-family:var(--display);font-size:27px;font-weight:800;"
        'letter-spacing:-.025em;line-height:1.2">Risk report</div>'
        "<div style=\"font-family:var(--mono);font-size:12px;color:var(--ink-3);"
        f'margin:2px 0 14px">{subtitle}</div>'
        "</div>"
        # Box metrics copied from the Team summary's switch (text-[11px],
        # tracking-wider, px-3 py-1.5, rounded-md) so the control is the same
        # size on both pages — only the active colour differs.
        '<div style="display:flex;border:1px solid var(--line);border-radius:6px;'
        "overflow:hidden;font-family:var(--mono);font-size:11px;font-weight:400;"
        'letter-spacing:.05em;text-transform:uppercase;line-height:1.5">'
        '<span style="background:#B42318;color:#FBEEEC;padding:6px 12px">Risk report</span>'
        f'<a href="{team_url}" style="background:var(--surface);color:var(--ink-2);'
        'padding:6px 12px;text-decoration:none">Team summary</a></div></div>'
    )


# Visual alignment of the two modes: the shadcn component idiom laid over the
# risk report's EXISTING markup. Purely cosmetic by construction — a stylesheet
# can rename nothing, remove nothing and rewire nothing, so every filter, tab,
# date-range and data object behaves exactly as before. Originally stand-only;
# released to the production /dashboard risk mode on 2026-08-25.
_STAND_SKIN = """<style id="stand-skin">
/* tab bar -> the same segmented control the Team summary uses, centred on the
   same 1180px column and out of the sticky layer, so the two headers align */
.dash-tabs{gap:0;position:static;height:auto;background:transparent;
  border-bottom:none;max-width:1180px;margin:0 auto;padding:6px 24px 10px}
.tab-btn{border:1px solid var(--line);border-radius:0;margin-left:-1px;
  background:var(--surface-2);padding:6px 14px}
.tab-btn:first-of-type{border-radius:6px 0 0 6px;margin-left:0}
.tab-btn:last-of-type{border-radius:0 6px 6px 0}
.tab-btn:hover{background:var(--surface)}
/* risk identity is RED here — the teal active state belongs to Team summary */
.tab-btn.active{background:var(--crit);color:#FBEEEC;border-color:var(--crit)}
.accent-bar{background:var(--crit)}
.tab-panel .sidebar{top:0;height:100vh}
/* the inner per-report heading steps down: the page title is the injected one */
.page-header h1{font-size:22px}
/* the injected header already carries the accent strip's job; the copies baked
   into each stored report body would stack up as duplicate bars */
.tab-panel .accent-bar{display:none}
/* header stat readouts -> the Team summary's tile cards. Weekly/monthly only:
   the daily digest has ~11 readouts and as cards they wrap into a wall — its
   compact strip layout is the correct form for that many. */
#panel-weekly .stat-strip,#panel-monthly .stat-strip{gap:12px;padding-bottom:22px}
#panel-weekly .stat-cell,#panel-monthly .stat-cell{background:var(--surface);
  border:1px solid var(--line);border-radius:8px;padding:13px 17px 14px;
  box-shadow:var(--shadow);min-width:128px}
#panel-weekly .stat-cell:first-child,#panel-monthly .stat-cell:first-child{
  padding-left:17px;border-left:1px solid var(--line)}
/* one corner radius across both modes */
.mgr-card,.cat-list,.dc-list,.portfolio-clean,.filter-panel{border-radius:8px}
</style>"""


#: Google Fonts tags served with the stored report bodies. The Team summary shell
#: ships no webfonts and falls back to the system stack — which the operator
#: prefers — so the risk mode strips these to make BOTH modes render with the
#: same faces. Frozen /r/{token} snapshot links keep their webfonts untouched.
_FONT_LINK_RE = re.compile(r'<link[^>]*fonts\.g(?:oogleapis|static)\.com[^>]*>')


async def _render_risk_report(
    day_arg: str | None, *, team_url: str, subtitle: str, cache_key: str, fresh: bool
) -> str:
    """The classic risk report, skinned and headed as one mode of a two-mode page.

    Renders the latest stored weekly and monthly plus a live daily panel, strips
    the webfonts, lays the alignment skin over the markup and injects the
    mirrored mode-switch header pointing back at ``team_url``. Touches none of
    the ``dashboards`` token machinery.

    TTL-cached under ``cache_key``: mode switching re-requests this page, and
    re-reading two ~250 KB stored reports per toggle is what made the switch
    feel slow. ``fresh=True`` bypasses.
    """
    if not fresh:
        cached = preview_cache.get(cache_key)
        if cached is not None:
            return cached

    async def _latest(period: str) -> str | None:
        async with acquire_connection() as conn:
            return await get_latest_summary_html(conn, period)

    # Three independent reads — fan out instead of three sequential round trips.
    weekly_html, monthly_html, daily_html = await asyncio.gather(
        _latest("weekly"), _latest("monthly"), _render_daily_panel(day_arg)
    )
    page = build_dashboard_html(
        weekly_html=weekly_html, monthly_html=monthly_html, daily_html=daily_html
    )
    # Same font faces as the Team summary: drop the webfont tags.
    page = _FONT_LINK_RE.sub("", page)
    # The visual-alignment skin goes last in <head> so it wins the cascade.
    head_at = page.find("</head>")
    if head_at != -1:
        page = page[:head_at] + _STAND_SKIN + page[head_at:]
    # The mirrored header strip goes below the red accent bar, above the tabs.
    header = _stand_header(team_url, subtitle)
    bar = '<div class="accent-bar"></div>'
    bar_at = page.find(bar)
    if bar_at != -1:
        insert_at = bar_at + len(bar)
        page = page[:insert_at] + header + page[insert_at:]
    else:  # unexpected markup — fall back to right after <body>
        body_at = page.find("<body")
        if body_at != -1:
            body_end = page.find(">", body_at)
            if body_end != -1:
                page = page[: body_end + 1] + header + page[body_end + 1 :]
    preview_cache.put(cache_key, page)
    return page


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
