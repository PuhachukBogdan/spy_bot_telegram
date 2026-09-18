"""Pydantic Settings, reads .env. Phase 1.

Single source of truth for all runtime configuration. Import the module-level
``settings`` singleton everywhere; never read os.environ directly.
"""

from __future__ import annotations

from datetime import date, time
from decimal import Decimal
from typing import Literal

from pydantic import SecretStr, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    """All runtime configuration, loaded from environment / ``.env``.

    Secret-bearing fields use ``SecretStr`` so they never leak into logs or
    reprs (see CLAUDE.md section 9: "Не логируем secret-поля").
    """

    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        case_sensitive=True,
        extra="ignore",
    )

    # === Telegram ===
    TELEGRAM_BOT_TOKEN: SecretStr
    TELEGRAM_WEBHOOK_SECRET: SecretStr
    # Auto-derived from SERVER_BASE_URL if not explicitly set.
    # DevOps only needs SERVER_BASE_URL; do NOT also set TELEGRAM_WEBHOOK_URL.
    TELEGRAM_WEBHOOK_URL: str = ""
    TELEGRAM_MANAGEMENT_CHAT_ID: int | None = None

    # === Supabase ===
    SUPABASE_URL: str
    SUPABASE_SERVICE_KEY: SecretStr
    # Contains DB credentials → kept secret even though not in the explicit
    # secret list; asyncpg consumers must call .get_secret_value().
    SUPABASE_DB_URL: SecretStr
    # Optional override for the DB password parsed from SUPABASE_DB_URL.
    # Set this when the password contains characters (e.g. +) that URL
    # encoding in SUPABASE_DB_URL would mangle. Takes precedence over the
    # password component of SUPABASE_DB_URL when present.
    SUPABASE_DB_PASSWORD: SecretStr | None = None
    SUPABASE_STORAGE_BUCKET: str = "llm-audit"

    # === LLM (OpenRouter) ===
    OPENROUTER_API_KEY: SecretStr
    LLM_MODEL_TIER2: str = "anthropic/claude-haiku-4-5"
    LLM_MODEL_SUMMARY: str = "anthropic/claude-sonnet-4-6"
    # One-off retrospective pass over imported archive history. Deliberately a
    # stronger model than the live Tier-2 default: that pass is judged on precision
    # by a human reading a management report, and the whole archive costs single-
    # digit dollars to analyse, so there is nothing to save by going cheaper.
    LLM_MODEL_RETRO: str = "anthropic/claude-sonnet-4-6"
    # Hard ceiling for one retro run, checked against OpenRouter's reported spend
    # before each call. Separate from DAILY_LLM_BUDGET_USD, which guards the live
    # pipeline's circuit breaker and must not be consumed by a one-off backfill.
    RETRO_BUDGET_USD: float = 25.0
    # The archive review is published on a PERMANENT link, deliberately outside the
    # weekly/monthly report machinery: those rotate their token on every generation
    # and revoke the previous one, which is exactly what must not happen here.
    #
    # Both values are required for the route to answer at all — it 404s unless each
    # is set. That is fail-closed on purpose: a link that never rotates and never
    # expires, carrying risk findings, is the highest-exposure artefact in the
    # system, so enabling it has to be a deliberate act rather than a default.
    ARCHIVE_REPORT_TOKEN: SecretStr | None = None
    ARCHIVE_REPORT_PASSWORD: SecretStr | None = None

    # === Whisper (OpenAI Audio API, separate from OpenRouter) ===
    OPENAI_API_KEY: SecretStr
    # MVP kill-switch. When false the queue consumer still runs and DRAINS
    # whisper_transcribe tasks (marks them done) without calling the paid API, so
    # the queue never backs up. Flip to true once the Whisper budget exists — no
    # code change needed. Voice notes queued while disabled are not transcribed
    # retroactively (they keep transcription=NULL).
    WHISPER_ENABLED: bool = False
    WHISPER_MODEL: str = "whisper-1"
    # Queue-consumer cadence (CLAUDE.md 7.5 priority lane uses 5s; Whisper is
    # slower + paid, so poll a touch less aggressively with a tiny batch).
    WHISPER_POLL_INTERVAL_SECONDS: int = 10
    WHISPER_BATCH_SIZE: int = 3
    WHISPER_MAX_ATTEMPTS: int = 3
    # OpenAI Audio API hard limit is 25 MB; skip anything larger than the API.
    WHISPER_MAX_FILE_BYTES: int = 25 * 1024 * 1024

    # === File analysis (document content risk detection) ===
    # Kill-switch: when false the worker drains the queue without spending.
    FILE_ANALYSIS_ENABLED: bool = True
    # Skip files larger than this (bytes); 20 MB covers most business docs.
    FILE_MAX_BYTES: int = 20 * 1024 * 1024
    # Truncate extracted text to this many chars before the LLM call.
    FILE_MAX_TEXT_CHARS: int = 40_000
    FILE_ANALYSIS_POLL_INTERVAL_SECONDS: int = 10
    FILE_ANALYSIS_BATCH_SIZE: int = 3
    FILE_ANALYSIS_MAX_ATTEMPTS: int = 3

    # === Slack ===
    SLACK_BOT_TOKEN: SecretStr
    SLACK_SIGNING_SECRET: SecretStr
    SLACK_CHANNEL_ALERTS: str
    SLACK_CHANNEL_REPORTS: str

    # === Cost limits (Decimal — money is never float, CLAUDE.md section 9) ===
    DAILY_LLM_BUDGET_USD: Decimal = Decimal("30")
    WEEKLY_LLM_BUDGET_USD: Decimal = Decimal("180")

    # === Pipeline ===
    BATCH_PROCESSING_INTERVAL_SECONDS: int = 600
    PRIORITY_SCORE_THRESHOLD: int = 50
    CONTEXT_WINDOW_MINUTES: int = 30
    ABANDONED_CHAT_TIMEOUT_HOURS: int = 168

    # === Risk scoring (final_score -> risk_level bands; pipeline §7.6) ===
    # Locked 2026-06-07 (risk-architecture session). A final_score at/above each
    # floor takes that level; anything below RISK_LEVEL_MEDIUM_MIN is 'low'. These
    # are the single source of truth — src.pipeline.scoring and the /thresholds
    # command both read them, so the bands can never drift between code and UI.
    RISK_LEVEL_MEDIUM_MIN: int = 30
    RISK_LEVEL_HIGH_MIN: int = 60
    RISK_LEVEL_CRITICAL_MIN: int = 80
    # Real-time alerts fire only at/above this level; lower levels are stored and
    # surface in the weekly/monthly summary instead. Locked: high + critical.
    ALERT_MIN_RISK_LEVEL: Literal["low", "medium", "high", "critical"] = "high"
    # Alert cooldown (Phase 11, legacy): superseded by RISK_CASE_WINDOW_MINUTES
    # below. Kept for config compatibility; the dispatch path no longer reads it.
    ALERT_COOLDOWN_MINUTES: int = 60
    # Risk-case window: a new alertable risk of the SAME type in the SAME chat
    # within this window belongs to the SAME open "case" — its Slack card is updated
    # in place (escalation) instead of posting a fresh top-level alert, so one case
    # is one card no matter how many messages it spans. Applies to critical too. A
    # risk type with no open case in the window opens a fresh card.
    RISK_CASE_WINDOW_MINUTES: int = 30
    # Failed-alert retry: a worker periodically re-posts undelivered Slack alerts
    # (the failed_alerts breadcrumbs) once Slack recovers, giving up after
    # FAILED_ALERT_MAX_RETRIES so a permanently-broken row isn't retried forever.
    FAILED_ALERT_RETRY_INTERVAL_SECONDS: int = 300
    FAILED_ALERT_MAX_RETRIES: int = 5

    # === Tier-2 analysis worker (unified batch + priority lane, decision A) ===
    # One per-chat analyze_chat task; the worker polls this often for due tasks
    # (immediate/bumped tasks need quick pickup). Per-tick claim size + max retries
    # mirror the whisper worker.
    ANALYSIS_POLL_INTERVAL_SECONDS: int = 15
    ANALYSIS_BATCH_SIZE: int = 5
    ANALYSIS_MAX_ATTEMPTS: int = 3
    # Window bounds for one analysis pass: at most this many new messages (since
    # the chat watermark) plus a few older ones for context.
    ANALYSIS_WINDOW_LIMIT: int = 60
    ANALYSIS_CONTEXT_BEFORE: int = 5
    # Cost gate: a tail pass waits for a real batch instead of burning an LLM call
    # on every trickle. It runs only once at least this many *significant* new
    # messages have accumulated — UNLESS a priority (Tier-1 >= PRIORITY_SCORE_
    # THRESHOLD) message is waiting, or the oldest unprocessed message is older
    # than ANALYSIS_MAX_WAIT_SECONDS (so a quiet chat is still analysed eventually,
    # the "ждём, но не вечно" rule).
    ANALYSIS_MIN_BATCH_MESSAGES: int = 5
    ANALYSIS_MAX_WAIT_SECONDS: int = 3600

    # === Stale task reaper ===
    # Tasks stuck in_progress longer than this are orphaned (worker crashed mid-run).
    # The reaper resets them to pending (or failed if attempts exhausted).
    STALE_TASK_TIMEOUT_SECONDS: int = 600   # 10 minutes
    STALE_TASK_REAPER_INTERVAL_SECONDS: int = 300  # run every 5 minutes

    # === SLA (response time — pure arithmetic, no LLM) ===
    # A message from anyone-but-staff starts a timer; the manager's first reply
    # stops it. Timers only START inside working hours (weekends and holidays
    # excluded), so plain wall-clock seconds are the measure — no cross-day
    # work-minute accumulation is needed.
    #
    # Four bands, in order of application (see src/metrics/sla.py):
    #   answered within THRESHOLD                      -> met
    #   slower, but the reply was SUBSTANTIVE, <= GRACE -> met (a real answer
    #                                                      takes longer to type
    #                                                      than "ок")
    #   anything else still inside OFFLINE_AFTER        -> missed
    #   nothing for OFFLINE_AFTER                       -> not slow, ABSENT;
    #                                                      counted separately and
    #                                                      never folded into the %
    SLA_RESPONSE_THRESHOLD_SECONDS: int = 120
    SLA_SUBSTANTIVE_GRACE_SECONDS: int = 300
    # ~2-3 sentences of Russian business chat. Sentences run 60-90 chars here, so
    # three land near 200; above that the manager was writing, not idling.
    SLA_SUBSTANTIVE_REPLY_CHARS: int = 200
    SLA_OFFLINE_AFTER_SECONDS: int = 1200

    # === Summary / HTML report (Phase 16) ===
    # Shared bearer token for POST /summary/generate and GET /reports/...
    # n8n passes it as ?token=; browser links carry it as a query param.
    SUMMARY_ACCESS_TOKEN: SecretStr = SecretStr("change-me-before-deploy")
    # Public base URL of this server, used to build the report link posted to Slack.
    SERVER_BASE_URL: str = "http://localhost:8080"
    # Timezone the reports live in. The weekly/monthly scheduler fires at 00:00
    # LOCAL time in this zone (not 08:00 UTC as before), the report window snaps
    # to that local midnight, and the daily-digest tab's calendar day rolls over
    # at local midnight too. DST is handled by zoneinfo, so the UTC instant
    # shifts with the season (Kyiv: 21:00 UTC in summer, 22:00 in winter).
    REPORT_TIMEZONE: str = "Europe/Kyiv"

    # === Phase 2 manager metrics (SLA %, active-chat KPI, tone of voice) ===
    # Hard floor for EVERY metrics window. Phase 2 KPIs count forward from the day
    # the code went to prod; nothing is computed retroactively (backfilling history
    # is a separate, later job).
    #
    # Set once at deploy, then left alone. Moving it forward discards accumulated
    # comparison history; moving it back lets the 56 329 imported archive messages
    # into the windows — they carry ORIGINAL timestamps running to 2026-08-03, so
    # they land squarely in the "previous period" of any period-over-period delta
    # and would make every manager look like they collapsed. Queries additionally
    # filter ``source <> 'imported'`` so that protection survives the backfill,
    # when windows start reaching back past this floor.
    #
    # None = no floor. Only correct before Phase 2 ships.
    METRICS_EPOCH_DATE: date | None = None

    # The bot's first two weeks in production (live from 2026-05-29) were spent
    # onboarding and testing — traffic from that stretch describes the rollout,
    # not the managers. Trend buckets that START before this date are flagged
    # ``test`` and drawn muted/labelled in charts, rather than silently mixed
    # into the same series as real work. None disables the marking.
    METRICS_TEST_PERIOD_UNTIL: date | None = date(2026, 6, 12)

    # Fallback working window, used ONLY for managers who have not set their own
    # via /set_hours. Measured 2026-08-15: 1 of 4 real managers had hours set, so
    # without this the other three would be measured around the clock and score
    # near zero against a 2-minute threshold — producing a ranking that reflects
    # who filled in a form, not who answers partners.
    #
    # Personal hours always win; this only fills the gap. Which of the two was
    # used is carried on every result (WorkHoursSource) and shown in the report,
    # so a number computed against an ASSUMED schedule is never presented as if
    # the manager had confirmed it.
    METRICS_DEFAULT_WORK_HOURS_START: time = time(9, 0)
    METRICS_DEFAULT_WORK_HOURS_END: time = time(18, 0)
    METRICS_DEFAULT_WORK_TIMEZONE: str = "Europe/Kyiv"

    # A chat counts as "active" for the coverage KPI at this many messages in the
    # reporting month. Low on purpose for now — the point is to separate live
    # chats from dead ones, not to set a performance bar.
    ACTIVE_CHAT_MIN_MESSAGES: int = 10

    # === Phase 2 preview stand ===
    # A SEPARATE link for reviewing the new metrics while the live weekly/monthly
    # report keeps running untouched. Same fail-closed pattern as the archive
    # link: both must be set or the route 404s. Renders live from the DB — it
    # stores nothing and touches none of the `summaries` / `dashboards` token
    # machinery, so it cannot disturb the report that is already in production.
    PREVIEW_REPORT_TOKEN: SecretStr | None = None
    PREVIEW_REPORT_PASSWORD: SecretStr | None = None

    # === Dashboard sign-in (2026-09-11) ===
    # The dashboard is no longer a shared token + password: a viewer signs in as
    # themselves (Telegram Login Widget, or a one-time link the bot DMs) and the
    # page is scoped to their role — see src/metrics/scope.py.
    # Signing key for the session cookie. Left unset it is derived from the bot
    # token, so no .env change is needed to deploy; setting (or changing) it
    # invalidates every outstanding session, which is the way to force re-login.
    DASHBOARD_SESSION_SECRET: SecretStr | None = None
    # Long on purpose: these are two or three internal people on their own
    # devices, and a page they have to re-authenticate weekly is a page they
    # stop opening. Revocation does not wait for it — /disable_user and
    # /set_role take effect on the viewer's next request.
    DASHBOARD_SESSION_DAYS: int = 90

    # Slack member ID -> role, applied ONCE when that Slack account finishes
    # /register. The dashboard is for two or three people who are not going to
    # be walked through a multi-step onboarding, so the grant makes their setup
    # "send /register, paste your Slack ID, paste the code" and nothing else.
    #
    # What actually gates this is NOT the id in the list: the one-time code is
    # delivered to that Slack account's DM, so only its owner can redeem an
    # entry. Knowing someone else's member ID buys nothing.
    #
    # Lives in .env, never in the repo: these are personal identifiers and the
    # tree is pushed to three GitHub remotes. Shape (one line):
    #   REGISTRATION_ROLE_GRANTS={"U01234ABCDE": "admin"}
    REGISTRATION_ROLE_GRANTS: dict[str, str] = {}

    # === Tone of voice (Phase 2, track F — daily LLM review of manager wording) ===
    # Kill switch. Off by default like OPS_ALERTS_ENABLED: a new LLM load is turned
    # on deliberately after its cost has been measured, never by a deploy.
    TONE_ANALYSIS_ENABLED: bool = False
    # The judgement is "clear cases only", which a small model handles well, and
    # the volume is a few dozen calls a day — switching to sonnet is one variable
    # if the calibration review shows misses.
    LLM_MODEL_TONE: str = "anthropic/claude-haiku-4-5"
    # Ceiling for the pass per UTC day, checked against OpenRouter's REPORTED spend
    # before each call. Separate from DAILY_LLM_BUDGET_USD (which it also counts
    # toward) so a runaway day cannot eat the live pipeline's headroom.
    TONE_DAILY_BUDGET_USD: Decimal = Decimal("2")
    # Flags below this confidence are dropped client-side even if the model returns
    # them — the prompt asks for 0.7; this enforces it rather than trusting it.
    TONE_MIN_CONFIDENCE: float = 0.7
    # A rate is shown only once this many manager messages were judged in the
    # period; below it the page says "too few messages" instead of 1-in-3 = 33 %.
    TONE_MIN_ASSESSED: int = 20
    # How many FINISHED days a tick may reach back for unprocessed chat-days —
    # covers an outage or a late enable without a separate backfill run. Today is
    # never judged: a day must be over to be judged whole.
    TONE_BACKFILL_DAYS: int = 7
    TONE_POLL_INTERVAL_SECONDS: int = 900
    # Lead-in context prepended to a day (the previous day's last messages), and
    # the window / overlap a very long day is split into.
    TONE_CONTEXT_MESSAGES: int = 10
    TONE_WINDOW_MESSAGES: int = 100
    TONE_WINDOW_OVERLAP: int = 10

    # === Ops Alerts (payment-provider incidents + Argentina holidays) ===
    # Master kill-switch: when false neither ops-alerts worker runs.
    OPS_ALERTS_ENABLED: bool = False
    # External RSS feed with payment-provider statuses. Sensitive — set at deploy,
    # never hardcoded. Empty disables the incidents branch even if OPS_ALERTS_ENABLED.
    OPS_FEED_URL: SecretStr | None = None
    OPS_INCIDENTS_POLL_INTERVAL_SECONDS: int = 600
    OPS_FEED_HTTP_RETRIES: int = 3
    OPS_FEED_HTTP_RETRY_DELAY_SECONDS: int = 5
    # Grace period after first detecting an active incident before we broadcast it
    # into partner groups. A payment provider that recovers inside this window
    # never reaches partners — a short dip is operationally insignificant and the
    # provider may already be back by the time an alert would land. Only an
    # incident still active past this delay (1.5h) is announced.
    OPS_INCIDENT_BROADCAST_DELAY_SECONDS: int = 5400
    # Argentina holiday reminder: checked daily at this local hour/timezone.
    OPS_HOLIDAYS_TIMEZONE: str = "Europe/Madrid"
    OPS_HOLIDAYS_CRON_HOUR: int = 13
    # How often the holiday loop wakes to check whether today's slot is due.
    OPS_HOLIDAYS_POLL_INTERVAL_SECONDS: int = 900
    # Max parallel group sends, to stay under Telegram's ~30 chats/sec limit.
    OPS_BROADCAST_SEMAPHORE: int = 20

    # === Storage monitoring (Supabase database size) ===
    # The Supabase plan's database-size cap in MB (free tier = 500 MB). A
    # background worker compares live ``pg_database_size()`` against this and,
    # once usage crosses STORAGE_ALERT_THRESHOLD_PERCENT, DMs every admin + posts
    # to Slack so we can purge/upgrade before writes are blocked. Raise this after
    # a plan upgrade.
    SUPABASE_DB_SIZE_LIMIT_MB: int = 500
    STORAGE_ALERT_THRESHOLD_PERCENT: int = 80
    # How often the monitor samples the DB size (6h — size moves slowly).
    STORAGE_MONITOR_INTERVAL_SECONDS: int = 21600
    # While still above the threshold, re-remind at most once per this many hours
    # so the alert doesn't repeat on every sample. Re-arms (warns again on the
    # next crossing) once usage drops back under the threshold.
    STORAGE_ALERT_REPING_HOURS: int = 24

    # === Bot ===
    BOT_DM_LANGUAGE: str = "en"
    ENVIRONMENT: Literal["production", "staging", "dev"] = "production"
    LOG_LEVEL: str = "INFO"

    @model_validator(mode="after")
    def _normalise_role_grants(self) -> Settings:
        """Upper-case the Slack ids and drop entries naming an unknown role.

        A typo in .env must not silently grant something, and it must not take
        the process down either: an unusable entry is dropped and the rest of
        the map still works. Roles mirror migration 0026's CHECK constraint.
        """
        allowed = {"admin", "head", "manager", "viewer"}
        cleaned: dict[str, str] = {}
        for key, raw_role in self.REGISTRATION_ROLE_GRANTS.items():
            role = raw_role.strip().lower()
            if role in allowed:
                cleaned[key.strip().upper()] = role
        self.REGISTRATION_ROLE_GRANTS = cleaned
        return self

    @model_validator(mode="after")
    def _derive_webhook_url(self) -> Settings:
        if not self.TELEGRAM_WEBHOOK_URL:
            self.TELEGRAM_WEBHOOK_URL = (
                f"{self.SERVER_BASE_URL.rstrip('/')}/webhook"
            )
        return self


# Singleton — import this everywhere. (The pydantic-settings mypy plugin knows
# env-sourced fields are populated at runtime, so no call-arg ignore is needed.)
settings = Settings()
