"""Pydantic Settings, reads .env. Phase 1.

Single source of truth for all runtime configuration. Import the module-level
``settings`` singleton everywhere; never read os.environ directly.
"""

from __future__ import annotations

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

    # === SLA (operational_sla track — time-based, no LLM) ===
    # A partner message with no internal reply for this many WORK minutes (counted
    # against the owning manager's work hours, migration 0008) raises an
    # operational_sla risk. The job lands in a later phase; the threshold lives in
    # config now so the value is fixed in one place.
    SLA_RESPONSE_THRESHOLD_MINUTES: int = 20

    # === Summary / HTML report (Phase 16) ===
    # Shared bearer token for POST /summary/generate and GET /reports/...
    # n8n passes it as ?token=; browser links carry it as a query param.
    SUMMARY_ACCESS_TOKEN: SecretStr = SecretStr("change-me-before-deploy")
    # Public base URL of this server, used to build the report link posted to Slack.
    SERVER_BASE_URL: str = "http://localhost:8080"

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
    # never reaches partners — a sub-hour dip is operationally insignificant and
    # the provider may already be back by the time an alert would land. Only an
    # incident still active past this delay is announced.
    OPS_INCIDENT_BROADCAST_DELAY_SECONDS: int = 3600
    # Argentina holiday reminder: checked daily at this local hour/timezone.
    OPS_HOLIDAYS_TIMEZONE: str = "Europe/Madrid"
    OPS_HOLIDAYS_CRON_HOUR: int = 13
    # How often the holiday loop wakes to check whether today's slot is due.
    OPS_HOLIDAYS_POLL_INTERVAL_SECONDS: int = 900
    # Max parallel group sends, to stay under Telegram's ~30 chats/sec limit.
    OPS_BROADCAST_SEMAPHORE: int = 20

    # === Bot ===
    BOT_DM_LANGUAGE: str = "en"
    ENVIRONMENT: Literal["production", "staging", "dev"] = "production"
    LOG_LEVEL: str = "INFO"

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
