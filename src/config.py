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

    # === Slack ===
    SLACK_BOT_TOKEN: SecretStr
    SLACK_SIGNING_SECRET: SecretStr
    SLACK_CHANNEL_ALERTS: str
    SLACK_CHANNEL_CRITICAL: str
    SLACK_CHANNEL_WEEKLY: str
    SLACK_CHANNEL_MONTHLY: str
    SLACK_CHANNEL_SYSTEM: str | None = None

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

    # === Bot ===
    BOT_DM_LANGUAGE: str = "en"
    ENVIRONMENT: Literal["production", "staging", "dev"] = "production"
    LOG_LEVEL: str = "INFO"


# Singleton — import this everywhere. (The pydantic-settings mypy plugin knows
# env-sourced fields are populated at runtime, so no call-arg ignore is needed.)
settings = Settings()
