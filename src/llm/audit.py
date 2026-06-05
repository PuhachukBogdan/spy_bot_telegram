"""Persist an LLM call: ``llm_calls`` row + best-effort Storage blobs. Phase 8.

Every Tier-2/priority call is recorded for cost tracking and forensic review. The
full prompt and response are archived to Supabase Storage (best-effort); the
``llm_calls`` row keeps only a SHA-256 of the prompt, a short response summary, the
blob paths, token/cost/latency, and any error. Recording the daily spend is a
separate step (:func:`src.db.queries.cost.record_llm_cost`) so a failed analysis
that still consumed tokens can be audited without being double-counted.
"""

from __future__ import annotations

import hashlib
from datetime import UTC, datetime
from decimal import Decimal
from uuid import UUID

import asyncpg

from src.db.models import LlmCall
from src.utils.logging import get_logger
from src.utils.storage import upload_text

log = get_logger(__name__)

# Response summaries are truncated to keep the row scannable; the full text lives
# in the Storage blob.
_SUMMARY_MAX = 500


def _sha256(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


async def record_llm_call(
    conn: asyncpg.Connection,
    *,
    call_type: str,
    model: str,
    chat_id: UUID | None,
    message_ids: list[UUID] | None,
    prompt_text: str,
    response_text: str,
    response_summary: str | None = None,
    tokens_in: int | None = None,
    tokens_out: int | None = None,
    cost_usd: Decimal | None = None,
    latency_ms: int | None = None,
    disagreement_flag: bool = False,
    error: str | None = None,
) -> LlmCall:
    """Archive blobs (best-effort) and insert one ``llm_calls`` row; return it."""
    prompt_hash = _sha256(prompt_text)
    day = datetime.now(UTC).strftime("%Y/%m/%d")
    base = f"{call_type}/{day}/{prompt_hash[:16]}"

    prompt_key = f"{base}.prompt.txt"
    prompt_path: str | None = (
        prompt_key if await upload_text(prompt_key, prompt_text) else None
    )
    response_key = f"{base}.response.json"
    response_path: str | None = (
        response_key
        if await upload_text(
            response_key, response_text, content_type="application/json; charset=utf-8"
        )
        else None
    )

    summary = response_summary or (response_text[:_SUMMARY_MAX] or None)

    row = await conn.fetchrow(
        """
        INSERT INTO llm_calls (
            call_type, model, chat_id, message_ids, prompt_hash,
            prompt_storage_path, response_summary, response_storage_path,
            tokens_in, tokens_out, cost_usd, latency_ms, disagreement_flag, error
        )
        VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9, $10, $11, $12, $13, $14)
        RETURNING *
        """,
        call_type,
        model,
        chat_id,
        message_ids,
        prompt_hash,
        prompt_path,
        summary,
        response_path,
        tokens_in,
        tokens_out,
        cost_usd,
        latency_ms,
        disagreement_flag,
        error,
    )
    assert row is not None  # INSERT ... RETURNING always yields a row
    log.info(
        "llm.call_recorded",
        call_type=call_type,
        model=model,
        cost_usd=str(cost_usd) if cost_usd is not None else None,
        error=error,
    )
    return LlmCall.from_record(row)
