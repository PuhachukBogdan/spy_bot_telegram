"""Document file-content analysis worker.

Consumes ``analyze_file`` tasks (one per document message) from
``processing_queue``. Each task downloads the file from Telegram, extracts
plain text (txt/csv/md/json/docx/pdf/xlsx/…), calls the LLM to detect
confidential-data leakage, and persists findings as ``risk_events`` rows
using the same scoring + alert-dispatch path as chat analysis.

Supported extraction formats (all run synchronously via ``asyncio.to_thread``):
  plain text — txt, md, csv, log, json, xml, yaml/yml, py, js, ts, sql, html
  docx       — python-docx
  pdf        — pypdf (text-layer only; scanned/image PDFs yield empty text)
  xlsx/xls   — openpyxl

Kill-switch: ``FILE_ANALYSIS_ENABLED=false`` drains the queue without spending.
Files above ``FILE_MAX_BYTES`` and unsupported formats are silently skipped.
"""

from __future__ import annotations

import asyncio
import io
import re
from datetime import UTC, datetime, timedelta
from uuid import UUID

from aiogram import Bot

from src.alerts.dispatch import dispatch_alerts
from src.config import settings
from src.db.client import acquire_connection
from src.db.models import Chat, Message, ProcessingQueue, RiskEvent
from src.db.queries.chats import get_chat_by_id
from src.db.queries.cost import record_llm_cost
from src.db.queries.messages import get_message_by_id
from src.db.queries.partners import get_partner_by_id
from src.db.queries.queue import complete_task, fail_task, retry_task
from src.db.queries.risk_events import save_risk_event
from src.llm.audit import record_llm_call
from src.llm.client import LlmFileResult, analyze_file_risk
from src.llm.prompts import load_template
from src.pipeline.scoring import score_finding, should_alert
from src.utils.logging import get_logger

log = get_logger(__name__)

_FILE_PROMPT = "file_risk_analysis"
_BACKOFF_BASE_SECONDS = 30

_PLAIN_TEXT_EXTS = {
    "txt", "md", "csv", "log", "json", "xml",
    "yaml", "yml", "py", "js", "ts", "sql", "html", "htm",
}

# Confirmed false positive (2026-07-06, CEO-reviewed): an affiliate COMMISSION /
# PAYOUT report is a routine partner deliverable. It legitimately carries player
# commission figures, transaction volumes and player IDs, which the file analyser
# otherwise reads as a critical data leak (business_secrets + internal_infra).
# Suppress it by FILENAME so the exclusion is precise to THIS one artifact type and
# never touches any other data_leak finding (chat leaks, credentials, strategy
# docs, other spreadsheets, …). Match e.g. "player_commission_report_06-07.xlsx",
# "Payout Report Q2".
_BENIGN_REPORT_RE = re.compile(r"(commission|payout)[\s._\-]*report", re.IGNORECASE)


def _is_benign_partner_report(file_name: str) -> bool:
    """True if the document is a routine partner commission/payout report."""
    return bool(_BENIGN_REPORT_RE.search(file_name))


class _NonRetryable(Exception):
    """Failure that retrying cannot fix."""


class _Skip(Exception):
    """Nothing to do — complete the task without spending."""


async def process_file_task(bot: Bot, task: ProcessingQueue) -> None:
    """Process one claimed ``analyze_file`` task end to end.

    Always resolves to a terminal state: done on success/skip, failed on a
    non-retryable error or exhausted attempts, back to pending with backoff
    on a transient error.
    """
    message_id_raw = task.payload.get("message_id")
    if not message_id_raw:
        async with acquire_connection() as conn:
            await fail_task(conn, task.id, "payload has no message_id")
        log.error("file_analysis.failed", task_id=task.id, error="no message_id")
        return

    message_id = UUID(str(message_id_raw))

    if not settings.FILE_ANALYSIS_ENABLED:
        async with acquire_connection() as conn:
            await complete_task(conn, task.id)
        log.info("file_analysis.skipped", task_id=task.id, reason="disabled")
        return

    try:
        alertable, chat = await _analyze_file(bot, message_id)
    except _Skip as exc:
        async with acquire_connection() as conn:
            await complete_task(conn, task.id)
        log.info(
            "file_analysis.skipped",
            task_id=task.id,
            message_id=str(message_id),
            reason=str(exc),
        )
        return
    except _NonRetryable as exc:
        async with acquire_connection() as conn:
            await fail_task(conn, task.id, str(exc))
        log.error(
            "file_analysis.failed",
            task_id=task.id,
            message_id=str(message_id),
            error=str(exc),
        )
        return
    except Exception as exc:
        await _reschedule_or_fail(task, message_id, str(exc))
        return

    async with acquire_connection() as conn:
        await complete_task(conn, task.id)

    # Post-commit dispatch — never inside the transaction.
    if alertable:
        async with acquire_connection() as conn:
            partner = (
                await get_partner_by_id(conn, chat.partner_id)
                if chat.partner_id is not None
                else None
            )
        partner_name = partner.name if partner is not None else None
        await dispatch_alerts(bot, chat, partner_name, alertable)

    log.info(
        "file_analysis.done",
        task_id=task.id,
        message_id=str(message_id),
        findings=len(alertable),
    )


async def _analyze_file(bot: Bot, message_id: UUID) -> tuple[list[RiskEvent], Chat]:
    """Download the file, extract text, call LLM, persist. May raise."""
    async with acquire_connection() as conn:
        message = await get_message_by_id(conn, message_id)
    if message is None:
        raise _NonRetryable("message row not found")

    async with acquire_connection() as conn:
        chat = await get_chat_by_id(conn, message.chat_id)
    if chat is None:
        raise _Skip("chat not found")

    file_id, file_name, mime_type = _media_ref(message)

    # Routine affiliate commission/payout report → confirmed-benign deliverable.
    # Skip before spending on download/extraction/LLM so it never becomes an alert.
    if _is_benign_partner_report(file_name):
        raise _Skip(f"benign partner report (routine deliverable): {file_name}")

    data = await _download(bot, file_id)

    if len(data) > settings.FILE_MAX_BYTES:
        raise _Skip(f"file too large: {len(data)} bytes")

    text = await asyncio.to_thread(_extract_text, data, file_name, mime_type)
    if text is None:
        raise _Skip(f"unsupported format: mime={mime_type!r} ext={file_name!r}")

    text = text[: settings.FILE_MAX_TEXT_CHARS]
    if not text.strip():
        raise _Skip("extracted text is empty")

    async with acquire_connection() as conn:
        system_prompt = await load_template(conn, _FILE_PROMPT)

    result = await analyze_file_risk(
        model=settings.LLM_MODEL_TIER2,
        system_prompt=system_prompt,
        file_name=file_name,
        file_content=text,
    )

    if not result.analysis.findings:
        # Still record cost even when the doc is clean.
        async with acquire_connection() as conn:
            if result.cost_usd is not None:
                await record_llm_cost(conn, result.cost_usd)
        return [], chat

    alertable = await _persist(chat, message, file_name, result)
    return alertable, chat


def _media_ref(message: Message) -> tuple[str, str, str]:
    raw = message.raw_payload or {}
    doc = raw.get("document")
    if not isinstance(doc, dict) or "file_id" not in doc:
        raise _NonRetryable("no document.file_id in raw_payload")
    return (
        str(doc["file_id"]),
        str(doc.get("file_name") or "unknown"),
        str(doc.get("mime_type") or ""),
    )


async def _download(bot: Bot, file_id: str) -> bytes:
    tg_file = await bot.get_file(file_id)
    if tg_file.file_path is None:
        raise _NonRetryable("Telegram returned no file_path")
    buf = await bot.download_file(tg_file.file_path)
    if buf is None:
        raise RuntimeError("download_file returned no buffer")
    return buf.read()


def _extract_text(data: bytes, file_name: str, mime_type: str) -> str | None:
    """Extract plain text from document bytes. Returns None for unsupported types.

    Runs synchronously — call via ``asyncio.to_thread`` to keep the event loop free.
    """
    ext = file_name.rsplit(".", 1)[-1].lower() if "." in file_name else ""

    if mime_type.startswith("text/") or ext in _PLAIN_TEXT_EXTS:
        return data.decode("utf-8", errors="replace")

    if (
        mime_type
        == "application/vnd.openxmlformats-officedocument.wordprocessingml.document"
        or ext == "docx"
    ):
        try:
            from docx import Document

            doc = Document(io.BytesIO(data))
            return "\n".join(p.text for p in doc.paragraphs)
        except Exception:
            return None

    if mime_type == "application/pdf" or ext == "pdf":
        try:
            import pypdf

            reader = pypdf.PdfReader(io.BytesIO(data))
            pages = [reader.pages[i].extract_text() or "" for i in range(len(reader.pages))]
            return "\n".join(pages)
        except Exception:
            return None

    if (
        mime_type
        in {
            "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
            "application/vnd.ms-excel",
        }
        or ext in {"xlsx", "xls"}
    ):
        try:
            import openpyxl

            wb = openpyxl.load_workbook(io.BytesIO(data), read_only=True, data_only=True)
            rows: list[str] = []
            for sheet in wb.worksheets:
                rows.append(f"[Sheet: {sheet.title}]")
                for row in sheet.iter_rows(values_only=True):
                    cells = [str(c) if c is not None else "" for c in row]
                    if any(cells):
                        rows.append("\t".join(cells))
            return "\n".join(rows)
        except Exception:
            return None

    return None


async def _persist(
    chat: Chat,
    message: Message,
    file_name: str,
    result: LlmFileResult,
) -> list[RiskEvent]:
    """Write audit row, cost, and ONE risk_events row for all findings. One transaction.

    All findings from a single file are collapsed into one risk event: the highest
    score drives the final_score; all explanations are concatenated so reviewers
    see the full picture in one Slack alert.
    """
    findings = result.analysis.findings
    # Sort highest-score first so the lead finding represents the worst signal.
    findings_sorted = sorted(findings, key=lambda f: f.score, reverse=True)
    lead = findings_sorted[0]

    rs = score_finding(
        rule_base_score=0,
        llm_score=lead.score,
        llm_confidence=lead.confidence,
    )

    # Combine all finding explanations into one readable block.
    explanation_lines = [
        f"[{f.category}] {f.explanation}" for f in findings_sorted
    ]
    combined_explanation = "\n".join(explanation_lines)

    alertable: list[RiskEvent] = []

    async with acquire_connection() as conn, conn.transaction():
        await record_llm_call(
            conn,
            call_type="file_llm",
            model=result.model,
            chat_id=chat.id,
            message_ids=[message.id],
            prompt_text=f"[file: {file_name}]",
            response_text=result.raw_response,
            tokens_in=result.tokens_in,
            tokens_out=result.tokens_out,
            cost_usd=result.cost_usd,
            latency_ms=result.latency_ms,
        )
        if result.cost_usd is not None:
            await record_llm_cost(conn, result.cost_usd)

        event = await save_risk_event(
            conn,
            risk_type="data_leak",
            risk_level=rs.risk_level,
            base_score=0,
            final_score=rs.final_score,
            message_id=message.id,
            partner_id=chat.partner_id,
            chat_id=chat.id,
            sender_id=message.sender_id,
            triggered_patterns=None,
            llm_confidence=lead.confidence,
            llm_multiplier=rs.llm_multiplier,
            llm_verdict=rs.llm_verdict,
            llm_explanation=combined_explanation,
            disagreement=False,
            detected_phrase=lead.excerpt[:200],
            context_message_ids=None,
        )
        if should_alert(rs.risk_level):
            alertable.append(event)

    return alertable


async def _reschedule_or_fail(
    task: ProcessingQueue, message_id: UUID, error: str
) -> None:
    if task.attempts >= settings.FILE_ANALYSIS_MAX_ATTEMPTS:
        async with acquire_connection() as conn:
            await fail_task(conn, task.id, error)
        log.error(
            "file_analysis.failed",
            task_id=task.id,
            message_id=str(message_id),
            error=error,
            attempts=task.attempts,
        )
        return
    delay = _BACKOFF_BASE_SECONDS * 2 ** (task.attempts - 1)
    run_at = datetime.now(UTC) + timedelta(seconds=delay)
    async with acquire_connection() as conn:
        await retry_task(conn, task.id, error, run_at)
    log.warning(
        "file_analysis.retry",
        task_id=task.id,
        message_id=str(message_id),
        attempts=task.attempts,
        delay_s=delay,
        error=error,
    )
