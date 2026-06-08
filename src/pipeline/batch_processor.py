"""Tier-2 batch analysis worker. Phase 9.

One unified per-chat lane (decision A, 2026-06-07): ingest keeps a single pending
``analyze_chat`` task per chat; a high Tier-1 score bumps it to run now, everything
else runs on the batch interval. This worker claims a due task and:

  1. loads the chat + its messages since the watermark (``chats.last_processed_at``)
     plus a little context (``messages.get_chat_analysis_window``);
  2. if nothing new is significant, just advances the watermark (no LLM spend);
  3. otherwise asks the LLM — the SOLE authority on severity — to find risks across
     the whole window (Tier-1 hits are passed as *flagged* hints only, not a
     verdict);
  4. scores each finding (:mod:`src.pipeline.scoring`: the rule score is a hint and
     never raises the result), writes a ``risk_events`` row per finding, records
     the LLM call + cost, and advances the watermark — all in one transaction;
  5. AFTER that transaction commits, dispatches the high+critical findings to Slack
     (:mod:`src.alerts.dispatch`) — outside any transaction, so a Slack network
     call never holds a DB connection.

The driving loop lives in :mod:`src.pipeline.workers`.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from uuid import UUID

from aiogram import Bot

from src.alerts.dispatch import dispatch_alerts
from src.config import settings
from src.db.client import acquire_connection
from src.db.models import Chat, Message, ProcessingQueue, RiskEvent
from src.db.queries.activity_signals import save_activity_signal
from src.db.queries.chats import get_chat_by_id, update_chat_last_processed
from src.db.queries.cost import record_llm_cost
from src.db.queries.messages import get_chat_analysis_window
from src.db.queries.partners import get_partner_by_id
from src.db.queries.queue import complete_task, defer_task, fail_task, retry_task
from src.db.queries.risk_events import save_risk_event
from src.llm.audit import record_llm_call
from src.llm.client import LlmResult, analyze_risk
from src.llm.prompts import (
    PromptNotFoundError,
    build_conversation_block,
    load_template,
)
from src.llm.schemas import RiskAnalysis, RiskFinding
from src.pipeline.scoring import RiskScore, score_finding, should_alert
from src.utils.logging import get_logger

log = get_logger(__name__)

_TIER2_PROMPT = "tier2_risk_analysis"
_BACKOFF_BASE_SECONDS = 30
# Detected-phrase snippet length stored on the risk row for the review surface.
_DETECTED_PHRASE_MAX = 200


class _Skip(Exception):
    """The task has nothing to do (chat gone): complete it, don't retry."""


class _Defer(Exception):
    """Not enough has accumulated yet: reschedule the task, don't spend the LLM."""


@dataclass(frozen=True)
class ScoredFinding:
    """One LLM finding paired with the message it targets and its computed score."""

    finding: RiskFinding
    message: Message
    score: RiskScore


def prepare_scored_findings(
    analysis: RiskAnalysis, new_messages: list[Message]
) -> list[ScoredFinding]:
    """Map LLM findings onto the new messages and score each (pure, no I/O).

    Only findings whose ``message_id`` is one of the *new* messages are kept: a
    finding that points at a context (already-processed) message is dropped to
    avoid creating a duplicate risk on a re-analysed message. The Tier-1
    ``base_score`` of the targeted message is passed to :func:`score_finding` as a
    hint — it never raises the final score.
    """
    by_id = {str(m.id): m for m in new_messages}
    scored: list[ScoredFinding] = []
    for finding in analysis.risk_events:
        message = by_id.get(finding.message_id)
        if message is None:
            log.info("analysis.finding_outside_window", message_id=finding.message_id)
            continue
        score = score_finding(
            rule_base_score=message.base_score,
            llm_score=finding.score,
            llm_confidence=finding.confidence,
        )
        scored.append(ScoredFinding(finding=finding, message=message, score=score))
    return scored


def should_defer_batch(new: list[Message], *, now: datetime) -> bool:
    """True if a tail pass should wait for a fuller batch (cost gate, pure).

    Defers only a *trickle*: at least one significant message but fewer than
    ``ANALYSIS_MIN_BATCH_MESSAGES`` of them, the oldest still younger than
    ``ANALYSIS_MAX_WAIT_SECONDS``, and nothing crossed the priority threshold. A
    priority hit, an old-enough backlog, or a full batch all force the pass
    through; an empty-of-significance window is the cost guard's job, not a defer.
    """
    if any(m.base_score >= settings.PRIORITY_SCORE_THRESHOLD for m in new):
        return False
    significant = [m for m in new if m.is_significant]
    if not significant or len(significant) >= settings.ANALYSIS_MIN_BATCH_MESSAGES:
        return False
    age_seconds = (now - new[0].created_at).total_seconds()
    return age_seconds < settings.ANALYSIS_MAX_WAIT_SECONDS


def _to_uuid_list(raw_ids: list[str]) -> list[UUID] | None:
    """Best-effort parse of LLM-returned id strings into UUIDs (skip malformed)."""
    out: list[UUID] = []
    for raw in raw_ids:
        try:
            out.append(UUID(raw))
        except (ValueError, AttributeError):
            continue
    return out or None


def _detected_phrase(message: Message) -> str | None:
    """A short snippet of the flagged message for the review surface."""
    text = message.message_text or message.transcription
    return text[:_DETECTED_PHRASE_MAX] if text else None


async def process_analysis_task(bot: Bot, task: ProcessingQueue) -> None:
    """Process one claimed ``analyze_chat`` task; always reach a terminal state."""
    chat_id_raw = task.payload.get("chat_id")
    if not chat_id_raw:
        async with acquire_connection() as conn:
            await fail_task(conn, task.id, "payload has no chat_id")
        log.error("analysis.failed", task_id=task.id, error="no chat_id")
        return
    chat_id = UUID(str(chat_id_raw))

    try:
        await _analyze_chat(bot, chat_id)
    except _Skip as exc:
        async with acquire_connection() as conn:
            await complete_task(conn, task.id)
        log.info("analysis.skipped", task_id=task.id, chat_id=str(chat_id), reason=str(exc))
        return
    except _Defer as exc:  # too little to analyse yet — wait for a fuller batch
        run_at = datetime.now(UTC) + timedelta(
            seconds=settings.BATCH_PROCESSING_INTERVAL_SECONDS
        )
        async with acquire_connection() as conn:
            await defer_task(conn, task.id, run_at)
        log.info(
            "analysis.deferred", task_id=task.id, chat_id=str(chat_id), reason=str(exc)
        )
        return
    except PromptNotFoundError as exc:  # config error — retrying won't help
        async with acquire_connection() as conn:
            await fail_task(conn, task.id, str(exc))
        log.error("analysis.failed", task_id=task.id, error=str(exc), retryable=False)
        return
    except Exception as exc:  # transient: LLM API, DB blip
        await _reschedule_or_fail(task, chat_id, str(exc))
        return

    async with acquire_connection() as conn:
        await complete_task(conn, task.id)
    log.info("analysis.task_done", task_id=task.id, chat_id=str(chat_id))


async def _reschedule_or_fail(task: ProcessingQueue, chat_id: UUID, error: str) -> None:
    """Back off and retry a transient failure, or give up once attempts run out."""
    if task.attempts >= settings.ANALYSIS_MAX_ATTEMPTS:
        async with acquire_connection() as conn:
            await fail_task(conn, task.id, error)
        log.error(
            "analysis.failed",
            task_id=task.id,
            chat_id=str(chat_id),
            error=error,
            retryable=True,
            attempts=task.attempts,
        )
        return
    delay = _BACKOFF_BASE_SECONDS * 2 ** (task.attempts - 1)
    run_at = datetime.now(UTC) + timedelta(seconds=delay)
    async with acquire_connection() as conn:
        await retry_task(conn, task.id, error, run_at)
    log.warning(
        "analysis.retry",
        task_id=task.id,
        chat_id=str(chat_id),
        attempts=task.attempts,
        delay_s=delay,
        error=error,
    )


async def _analyze_chat(bot: Bot, chat_id: UUID) -> None:
    """Analyse one chat's new messages end to end. May raise (transient -> retry)."""
    async with acquire_connection() as conn:
        chat = await get_chat_by_id(conn, chat_id)
        if chat is None:
            raise _Skip("chat row not found")
        context, new = await get_chat_analysis_window(
            conn,
            chat_id,
            since=chat.last_processed_at,
            limit=settings.ANALYSIS_WINDOW_LIMIT,
            context_before=settings.ANALYSIS_CONTEXT_BEFORE,
        )

    if not new:
        return  # nothing new since the watermark

    # The cursor is created_at (migration 0010), so the watermark advances to the
    # newest message's created_at — second-granular Telegram timestamps can no
    # longer collide at the boundary and silently drop a same-second neighbour.
    newest_ts = new[-1].created_at

    # Cost guard: if no new message carries analysable text, skip the LLM and just
    # advance the watermark (stickers/joins/etc. don't need Tier-2).
    if not any(m.is_significant for m in new):
        async with acquire_connection() as conn:
            await update_chat_last_processed(conn, chat_id, newest_ts)
        log.info("analysis.insignificant", chat_id=str(chat_id), new=len(new))
        return

    # Cost gate: hold a trickle until it grows into a real batch (or ages out, or a
    # priority message forces it). The watermark is deliberately NOT advanced, so
    # these messages are re-evaluated on the next pass.
    if should_defer_batch(new, now=datetime.now(UTC)):
        raise _Defer(f"{sum(m.is_significant for m in new)} significant, awaiting batch")

    async with acquire_connection() as conn:
        system_prompt = await load_template(conn, _TIER2_PROMPT)

    window = context + new
    flagged_ids = [m.id for m in new if m.base_score > 0]
    conversation_block = build_conversation_block(window, flagged_ids)

    # LLM call OUTSIDE any transaction (don't hold a DB lock across the API call).
    result = await analyze_risk(
        model=settings.LLM_MODEL_TIER2,
        system_prompt=system_prompt,
        conversation_block=conversation_block,
    )
    scored = prepare_scored_findings(result.analysis, new)

    alertable = await _persist(chat, new, conversation_block, result, scored, newest_ts)

    # Dispatch AFTER the transaction has committed — never inside it (a Slack
    # network call must not hold a DB connection). A crash between commit and here
    # leaves the risk persisted but un-alerted; that's the rare, acceptable edge.
    if alertable:
        async with acquire_connection() as conn:
            partner = (
                await get_partner_by_id(conn, chat.partner_id)
                if chat.partner_id is not None
                else None
            )
        partner_name = partner.name if partner is not None else None
        await dispatch_alerts(bot, chat, partner_name, alertable)


async def _persist(
    chat: Chat,
    new_messages: list[Message],
    prompt_text: str,
    result: LlmResult,
    scored: list[ScoredFinding],
    newest_ts: datetime,
) -> list[RiskEvent]:
    """Write the LLM-call audit row, cost, every risk_events row, activity signals,
    and the watermark.

    One transaction so a chat never gets risk rows without its watermark advancing
    (which would re-analyse and duplicate them) and vice-versa. Returns the saved
    risk events that should fire a real-time alert (high+critical), for the caller
    to dispatch once this transaction has committed.
    """
    any_disagreement = any(s.score.disagreement for s in scored)
    message_ids = [m.id for m in new_messages]
    alertable: list[RiskEvent] = []
    msg_by_id = {str(m.id): m for m in new_messages}

    async with acquire_connection() as conn, conn.transaction():
        await record_llm_call(
            conn,
            call_type="batch_llm",
            model=result.model,
            chat_id=chat.id,
            message_ids=message_ids,
            prompt_text=prompt_text,
            response_text=result.raw_response,
            tokens_in=result.tokens_in,
            tokens_out=result.tokens_out,
            cost_usd=result.cost_usd,
            latency_ms=result.latency_ms,
            disagreement_flag=any_disagreement,
        )
        if result.cost_usd is not None:
            await record_llm_cost(conn, result.cost_usd)

        for s in scored:
            event = await save_risk_event(
                conn,
                risk_type=s.finding.risk_type.value,
                risk_level=s.score.risk_level,
                base_score=s.score.base_score,
                final_score=s.score.final_score,
                message_id=s.message.id,
                partner_id=chat.partner_id,
                chat_id=chat.id,
                sender_id=s.message.sender_id,
                triggered_patterns=s.message.triggered_patterns,
                llm_confidence=s.finding.confidence,
                llm_multiplier=s.score.llm_multiplier,
                llm_verdict=s.score.llm_verdict,
                llm_explanation=s.finding.explanation,
                disagreement=s.score.disagreement,
                detected_phrase=_detected_phrase(s.message),
                context_message_ids=_to_uuid_list(s.finding.context_message_ids),
            )
            if should_alert(s.score.risk_level):
                alertable.append(event)

        for sig in result.analysis.activity_signals:
            msg = msg_by_id.get(sig.message_id)
            if msg is None:
                log.info("analysis.signal_outside_window", message_id=sig.message_id)
                continue
            await save_activity_signal(
                conn,
                chat_id=chat.id,
                message_id=msg.id,
                sender_id=msg.sender_id,
                signal_type=sig.signal_type.value,
                description=sig.description,
            )

        await update_chat_last_processed(conn, chat.id, newest_ts)

    log.info(
        "analysis.done",
        chat_id=str(chat.id),
        new=len(new_messages),
        findings=len(scored),
        signals=len(result.analysis.activity_signals),
        alerts=len(alertable),
    )
    return alertable
