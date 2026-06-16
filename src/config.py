"""Pydantic Settings, reads .env. Phase 1.

Single source of truth for all runtime configuration. Import the module-level
``settings`` singleton everywhere; never read os.environ directly.
"""

from __future__ import annotations

from decimal import Decimal
from typing import Literal

from pydantic import SecretStr
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
    TELEGRAM_WEBHOOK_URL: str
    TELEGRAM_MANAGEMENT_CHAT_ID: int | None = None

    # === Supabase ===
    SUPABASE_URL: str
    SUPABASE_SERVICE_KEY: SecretStr
    # Contains DB credentials → kept secret even though not in the explicit
    # secret list; asyncpg consumers must call .get_secret_value().
    SUPABASE_DB_URL: SecretStr
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

    # === n8n system webhook ===
    N8N_SYSTEM_WEBHOOK_URL: str | None = None

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
    # Alert cooldown (Phase 11): a repeat risk of the SAME type in the SAME chat
    # within this window is threaded under the prior Slack alert instead of firing a
    # fresh channel ping. Critical alerts and a never-seen risk type bypass it.
    ALERT_COOLDOWN_MINUTES: int = 60

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

    # === Bot ===
    BOT_DM_LANGUAGE: str = "en"
    ENVIRONMENT: Literal["production", "staging", "dev"] = "production"
    LOG_LEVEL: str = "INFO"


# Singleton — import this everywhere. (The pydantic-settings mypy plugin knows
# env-sourced fields are populated at runtime, so no call-arg ignore is needed.)
settings = Settings()
