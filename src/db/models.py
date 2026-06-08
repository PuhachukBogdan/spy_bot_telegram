"""Pydantic v2 models for all 17 tables. Phase 2.

One model per table in ``supabase/migrations/0001_initial_schema.sql``. These
are primarily *row* models: build them from an ``asyncpg.Record`` with
``Model.from_record(record)`` (see ``_ORMModel`` for why a plain
``model_validate(record)`` does not work on a Record).

Type mapping conventions:
  - PK / FK / UUID columns  -> ``uuid.UUID``        (see note below)
  - ``UUID[]``              -> ``list[UUID] | None``
  - ``TEXT[]``             -> ``list[str] | None``
  - ``TIMESTAMPTZ``        -> ``datetime``
  - ``DATE``               -> ``date``
  - ``NUMERIC``            -> ``Decimal`` (never float, CLAUDE.md section 9)
  - ``JSONB``              -> ``dict[str, Any]`` / ``list[Any]`` per content
  - ``BIGINT`` / ``INT``   -> ``int``
  - ``FLOAT``              -> ``float`` (llm_confidence / llm_multiplier only)

Why ``uuid.UUID`` and not ``pydantic.UUID4``:
  asyncpg decodes ``UUID`` columns to native ``uuid.UUID`` objects, so this is a
  zero-conversion match on read. ``UUID4`` additionally asserts version==4;
  although ``gen_random_uuid()`` does emit v4, that extra check buys nothing on
  trusted DB-sourced data and would reject any legitimately stored non-v4 id.
  Plain ``UUID`` still serializes to a string in JSON output.

Columns that are NOT NULL with a server-side DEFAULT are given the same default
here so a model can be constructed before insert; columns that are NOT NULL with
no default are required; nullable columns default to ``None``.
"""

from __future__ import annotations

from collections.abc import Mapping
from datetime import date, datetime, time
from decimal import Decimal
from typing import Any, Literal, Self
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field


class _ORMModel(BaseModel):
    """Base for every table model.

    Use :meth:`from_record` to build from an ``asyncpg.Record``. A Record is a
    *mapping*, not an attribute object, so ``model_validate(record)`` with
    ``from_attributes=True`` fails (Pydantic reads fields via ``getattr``, which
    a Record does not support for columns). ``from_record`` converts to a plain
    dict first, which validates by mapping. ``from_attributes`` is kept for any
    genuine attribute-style source.
    """

    model_config = ConfigDict(from_attributes=True)

    @classmethod
    def from_record(cls, record: Mapping[str, Any]) -> Self:
        """Validate from an ``asyncpg.Record`` (or any mapping) by dict-coercion."""
        return cls.model_validate(dict(record))


# --- 1. internal_users ------------------------------------------------------
class InternalUser(_ORMModel):
    id: UUID
    full_name: str
    # role is the single source of truth for access (migration 0007). The legacy
    # is_admin column still exists in the DB but is ignored on read (extra keys
    # are dropped) — is_admin is exposed below as a property derived from role.
    role: Literal["admin", "manager", "viewer"] = "manager"
    telegram_accounts: list[int] = Field(default_factory=list)
    enabled: bool = True
    # Migration 0008: working hours for the operational_sla track. start/end are
    # NULL until the user runs /set_hours; work_timezone is an IANA name and
    # always present (DEFAULT 'UTC'). asyncpg decodes TIME -> datetime.time.
    work_hours_start: time | None = None
    work_hours_end: time | None = None
    work_timezone: str = "UTC"
    created_at: datetime

    @property
    def is_admin(self) -> bool:
        """Back-compat shim: admin iff role == 'admin' (CLAUDE.md / migration 0007)."""
        return self.role == "admin"


# --- 2. partners ------------------------------------------------------------
class Partner(_ORMModel):
    id: UUID
    name: str
    status: str = "active"
    owner_manager_id: UUID | None = None
    created_at: datetime
    updated_at: datetime


# --- 3. chats ---------------------------------------------------------------
class Chat(_ORMModel):
    id: UUID
    telegram_chat_id: int
    # A chats row is a monitored *unit* = one forum topic. message_thread_id is
    # NULL for a whole group / forum General topic / non-forum group; topic_key
    # is the generated COALESCE(message_thread_id, 0) used for NULL-safe
    # uniqueness (read-only — never supplied on insert).
    message_thread_id: int | None = None
    topic_key: int = 0
    topic_name: str | None = None
    partner_id: UUID | None = None
    chat_name: str | None = None
    status: str = "pending"
    added_by_user_id: int | None = None
    authorized_by: UUID | None = None
    authorized_at: datetime | None = None
    chat_purpose: str | None = None
    # Migration 0006: unit typing + Telegram Business binding. For a business
    # unit, telegram_chat_id holds the partner's TG user_id (a private chat has
    # chat.id == user.id).
    unit_type: Literal["group", "topic", "business"] = "group"
    business_connection_id: str | None = None
    business_peer_user_id: int | None = None
    # Migration 0009: Tier-2 analysis watermark. Messages newer than this have not
    # been sent to the LLM yet; the batch worker advances it after each pass.
    last_processed_at: datetime | None = None
    created_at: datetime


# --- 4. messages ------------------------------------------------------------
class Message(_ORMModel):
    id: UUID
    telegram_message_id: int
    chat_id: UUID
    sender_id: int | None = None
    sender_chat_id: int | None = None
    sender_name: str | None = None
    sender_role: str
    message_text: str | None = None
    message_type: str
    timestamp: datetime
    reply_to_message_id: int | None = None
    forward_from_id: int | None = None
    forward_from_chat_id: int | None = None
    message_thread_id: int | None = None
    links: list[str] | None = None
    mentions: list[str] | None = None
    detected_language: str | None = None
    transcription: str | None = None
    is_significant: bool = False
    has_triggers: bool = False
    triggered_patterns: dict[str, Any] | list[Any] | None = None
    base_score: int = 0
    source: str = "live"  # live / live_group / live_topic / business / imported
    raw_payload: dict[str, Any] | None = None
    # Migration 0006: business provenance + soft-deletion record.
    business_connection_id: str | None = None
    business_peer_user_id: int | None = None
    deleted_at: datetime | None = None
    deletion_payload: dict[str, Any] | None = None
    # Ingestion time, and also the Tier-2 analysis cursor (migration 0010): the
    # batch worker windows on created_at, and a threshold-crossing edit bumps it to
    # now() so the edited message is re-analysed. The true send-time is ``timestamp``.
    created_at: datetime


# --- 5. message_edits -------------------------------------------------------
class MessageEdit(_ORMModel):
    id: UUID
    message_id: UUID
    old_text: str | None = None
    new_text: str | None = None
    edited_at: datetime
    created_at: datetime


# --- 6. chat_events ---------------------------------------------------------
class ChatEvent(_ORMModel):
    id: UUID
    chat_id: UUID
    event_type: str
    actor_user_id: int | None = None
    target_user_id: int | None = None
    payload: dict[str, Any] | None = None
    created_at: datetime


# --- 7. risk_events ---------------------------------------------------------
class RiskEvent(_ORMModel):
    id: UUID
    message_id: UUID | None = None
    partner_id: UUID | None = None
    chat_id: UUID | None = None
    sender_id: int | None = None
    risk_type: str
    risk_level: str
    triggered_patterns: dict[str, Any] | list[Any] | None = None
    context_modifiers: dict[str, Any] | None = None
    base_score: int
    llm_confidence: float | None = None
    llm_multiplier: float | None = None
    llm_verdict: str | None = None
    llm_explanation: str | None = None
    final_score: int
    disagreement: bool = False
    detected_phrase: str | None = None
    context_message_ids: list[UUID] | None = None
    status: str = "new"
    reviewed_by: UUID | None = None
    reviewed_at: datetime | None = None
    slack_message_ts: str | None = None
    created_at: datetime


# --- 8. red_flag_patterns ---------------------------------------------------
class RedFlagPattern(_ORMModel):
    id: UUID
    pattern: str
    pattern_type: str = "literal"
    language: str
    risk_category: str
    base_score: int
    examples: list[str] | None = None
    enabled: bool = True
    updated_at: datetime


# --- 9. summaries -----------------------------------------------------------
class Summary(_ORMModel):
    id: UUID
    partner_id: UUID | None = None
    period_type: str
    period_start: datetime
    period_end: datetime
    structured_content: dict[str, Any]
    rendered_html: str | None = None
    risk_event_ids: list[UUID] | None = None
    action_items: dict[str, Any] | list[Any] | None = None
    delivery_status: str = "pending"
    delivered_at: datetime | None = None
    created_at: datetime


# --- 10. summaries_skipped --------------------------------------------------
class SummarySkipped(_ORMModel):
    id: UUID
    partner_id: UUID | None = None
    period_type: str
    period_start: datetime
    period_end: datetime
    reason: str
    significant_message_count: int | None = None
    risk_event_count: int | None = None
    created_at: datetime


# --- 11. critical_alert_recipients ------------------------------------------
class CriticalAlertRecipient(_ORMModel):
    id: UUID
    full_name: str
    slack_user_id: str | None = None
    email: str | None = None
    enabled: bool = True
    added_at: datetime


# --- 12. admin_audit_log ----------------------------------------------------
class AdminAuditLog(_ORMModel):
    id: UUID
    actor_user_id: int | None = None
    actor_internal_id: UUID | None = None
    action: str
    target_entity: str | None = None
    target_id: UUID | None = None
    payload: dict[str, Any] | None = None
    ip: str | None = None
    created_at: datetime


# --- 13. llm_calls ----------------------------------------------------------
class LlmCall(_ORMModel):
    id: UUID
    call_type: str
    model: str
    chat_id: UUID | None = None
    message_ids: list[UUID] | None = None
    prompt_hash: str
    prompt_storage_path: str | None = None
    response_summary: str | None = None
    response_storage_path: str | None = None
    tokens_in: int | None = None
    tokens_out: int | None = None
    cost_usd: Decimal | None = None
    latency_ms: int | None = None
    disagreement_flag: bool = False
    error: str | None = None
    created_at: datetime


# --- 14. prompts ------------------------------------------------------------
class Prompt(_ORMModel):
    id: UUID
    name: str
    version: int
    template: str
    json_schema: dict[str, Any] | None = None
    active: bool = False
    notes: str | None = None
    created_by: str | None = None
    created_at: datetime


# --- 15. failed_alerts ------------------------------------------------------
class FailedAlert(_ORMModel):
    id: UUID
    risk_event_id: UUID | None = None
    channel: str
    payload: dict[str, Any] | None = None
    error: str | None = None
    retry_count: int = 0
    last_attempt_at: datetime | None = None
    resolved: bool = False
    created_at: datetime


# --- 16. cost_tracking ------------------------------------------------------
class CostTracking(_ORMModel):
    date: date
    llm_cost_usd: Decimal = Decimal("0")
    whisper_cost_usd: Decimal = Decimal("0")
    # GENERATED ALWAYS column: always present on read, never supplied on write.
    total_cost_usd: Decimal | None = None
    llm_calls_count: int = 0
    whisper_calls_count: int = 0
    circuit_breaker_triggered: bool = False


# --- 17. processing_queue ---------------------------------------------------
class ProcessingQueue(_ORMModel):
    id: int  # BIGSERIAL
    task_type: str
    payload: dict[str, Any]
    status: str = "pending"
    attempts: int = 0
    last_attempt_at: datetime | None = None
    error: str | None = None
    scheduled_for: datetime
    created_at: datetime
    completed_at: datetime | None = None


# === Migration 0006: topics & Telegram Business mode ========================
# These four models double as create() input payloads, so the server-generated
# id / created_at are optional (None before insert, populated by RETURNING *).


# --- 18. business_connections -----------------------------------------------
class BusinessConnection(_ORMModel):
    id: UUID | None = None
    business_connection_id: str
    business_account_user_id: int
    internal_user_id: UUID | None = None
    status: Literal["pending", "active", "revoked", "disabled"] = "pending"
    rights: dict[str, Any] = Field(default_factory=dict)
    connected_at: datetime
    revoked_at: datetime | None = None
    approved_by: UUID | None = None
    approved_at: datetime | None = None
    raw_payload: dict[str, Any] | None = None
    created_at: datetime | None = None


# --- 19. partner_contacts ---------------------------------------------------
class PartnerContact(_ORMModel):
    id: UUID | None = None
    partner_id: UUID
    telegram_user_id: int
    full_name: str | None = None
    notes: str | None = None
    created_at: datetime | None = None


# --- 20. notes --------------------------------------------------------------
class Note(_ORMModel):
    id: UUID | None = None
    partner_id: UUID
    chat_id: UUID | None = None
    note_type: Literal["general", "handoff", "open_question"]
    content: str
    created_by: UUID
    created_at: datetime | None = None
    resolved_at: datetime | None = None
    resolved_by: UUID | None = None


# --- 21. reminders ----------------------------------------------------------
class Reminder(_ORMModel):
    id: UUID | None = None
    target_user_id: UUID
    partner_id: UUID | None = None
    content: str
    fire_at: datetime
    status: Literal["pending", "sent", "cancelled", "failed"] = "pending"
    created_by: UUID
    created_at: datetime | None = None
    sent_at: datetime | None = None


# === Read projections =======================================================
# Not table rows: aggregate / joined shapes returned by the DM listing commands
# (partners / chats / risks). Kept typed (CLAUDE.md section 9 — no dict[str, Any]
# in public APIs) so handlers read fields, not Record keys.


class PartnerOverview(_ORMModel):
    """One row of ``/partners``: a partner plus activity rollups."""

    id: UUID
    name: str
    status: str
    owner_manager_id: UUID | None = None
    active_chats: int = 0
    last_activity: datetime | None = None


class ChatOverview(_ORMModel):
    """One row of ``/chats``: a unit plus its partner name and last activity."""

    id: UUID
    telegram_chat_id: int
    unit_type: str
    message_thread_id: int | None = None
    chat_name: str | None = None
    status: str
    partner_name: str | None = None
    last_activity: datetime | None = None


class ChatAdderSummary(_ORMModel):
    """One row of the admin panel home: an internal user who connected chats."""

    internal_user_id: UUID
    full_name: str
    role: str
    chat_count: int = 0


class RiskEventOverview(_ORMModel):
    """One row of ``/risks``: a risk event plus its partner name."""

    id: UUID
    risk_level: str
    risk_type: str
    detected_phrase: str | None = None
    status: str
    created_at: datetime
    partner_name: str | None = None
