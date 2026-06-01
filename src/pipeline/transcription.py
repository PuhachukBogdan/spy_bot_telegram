"""Whisper transcription worker. Phase 7.

Consumes ``whisper_transcribe`` tasks off ``processing_queue`` (enqueued by
ingest for every voice / video_note, CLAUDE.md 7.1 step 8), turns the audio into
text via the OpenAI Audio API, writes it to ``messages.transcription``, then
re-runs Tier-1 on the transcript so a spoken risk phrase is scored exactly like a
typed one (CLAUDE.md Phase 7 / 7.1).

MVP kill-switch: when ``settings.WHISPER_ENABLED`` is false the worker still
drains the queue (marks each task done) but never calls the paid API, so the
queue can't back up while transcription is off. Flip the flag to true once the
Whisper budget exists — no code change. Voice notes that arrived while disabled
are not transcribed retroactively (they keep ``transcription = NULL``).

ffmpeg is needed only for video_note (extract the audio track from the .mp4);
voice notes are .ogg/opus and go straight to the API. ffmpeg ships in the
Docker image; on a host without it, video_note tasks fail (non-retryable) while
voice still works.

The loop that drives this worker lives in ``pipeline.workers`` alongside the
other background loops.
"""

from __future__ import annotations

import asyncio
import contextlib
import os
import shutil
import subprocess
import tempfile
from datetime import UTC, datetime, timedelta
from decimal import ROUND_HALF_UP, Decimal
from uuid import UUID

from aiogram import Bot
from openai import AsyncOpenAI

from src.config import settings
from src.db.client import acquire_connection
from src.db.models import Message, ProcessingQueue
from src.db.queries.cost import record_whisper_cost
from src.db.queries.messages import (
    get_message_by_id,
    update_message_transcription,
    update_message_triggers,
)
from src.db.queries.queue import complete_task, enqueue_task, fail_task, retry_task
from src.pipeline.tier1 import pattern_cache
from src.utils.logging import get_logger

log = get_logger(__name__)

# whisper-1 list price (USD per minute of audio). Used for cost_tracking only.
_WHISPER_USD_PER_MINUTE = Decimal("0.006")
# Retry backoff base: delay = _BACKOFF_BASE_SECONDS * 2**(attempts-1).
_BACKOFF_BASE_SECONDS = 30

# Lazy OpenAI client singleton (same rationale as the asyncpg pool in
# src.db.client: one shared client, built on first use, not a hardcoded global).
_client: AsyncOpenAI | None = None


class _NonRetryable(Exception):
    """A failure that retrying won't fix (bad data, missing ffmpeg, oversize)."""


def _get_client() -> AsyncOpenAI:
    """Return the process-wide OpenAI client, creating it on first use."""
    global _client
    if _client is None:
        _client = AsyncOpenAI(api_key=settings.OPENAI_API_KEY.get_secret_value())
    return _client


async def process_whisper_task(bot: Bot, task: ProcessingQueue) -> None:
    """Process one claimed ``whisper_transcribe`` task end to end.

    Always resolves the task to a terminal state: ``done`` on success or skip,
    ``failed`` on a non-retryable error or once attempts are exhausted, or back to
    ``pending`` with a backoff for a transient error.
    """
    message_id_raw = task.payload.get("message_id")
    if not message_id_raw:
        async with acquire_connection() as conn:
            await fail_task(conn, task.id, "payload has no message_id")
        log.error("whisper.failed", task_id=task.id, error="no message_id")
        return
    message_id = UUID(str(message_id_raw))

    # MVP kill-switch: drain without spending.
    if not settings.WHISPER_ENABLED:
        async with acquire_connection() as conn:
            await complete_task(conn, task.id)
        log.info(
            "whisper.skipped",
            task_id=task.id,
            message_id=str(message_id),
            reason="disabled",
        )
        return

    try:
        await _transcribe_message(bot, message_id)
    except _NonRetryable as exc:
        async with acquire_connection() as conn:
            await fail_task(conn, task.id, str(exc))
        log.error(
            "whisper.failed",
            task_id=task.id,
            message_id=str(message_id),
            error=str(exc),
            retryable=False,
        )
        return
    except Exception as exc:  # transient: network, API 5xx, ffmpeg crash
        await _reschedule_or_fail(task, message_id, str(exc))
        return

    async with acquire_connection() as conn:
        await complete_task(conn, task.id)
    log.info("whisper.done", task_id=task.id, message_id=str(message_id))


async def _reschedule_or_fail(
    task: ProcessingQueue, message_id: UUID, error: str
) -> None:
    """Back off and retry a transient failure, or give up once attempts run out.

    ``task.attempts`` was already incremented by ``claim_tasks``, so it is the
    number of tries so far including this one.
    """
    if task.attempts >= settings.WHISPER_MAX_ATTEMPTS:
        async with acquire_connection() as conn:
            await fail_task(conn, task.id, error)
        log.error(
            "whisper.failed",
            task_id=task.id,
            message_id=str(message_id),
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
        "whisper.retry",
        task_id=task.id,
        message_id=str(message_id),
        attempts=task.attempts,
        delay_s=delay,
        error=error,
    )


async def _transcribe_message(bot: Bot, message_id: UUID) -> None:
    """Download, transcribe, persist, and re-score one message. May raise."""
    async with acquire_connection() as conn:
        message = await get_message_by_id(conn, message_id)
    if message is None:
        raise _NonRetryable("message row not found")

    file_id, duration_s = _media_ref(message)
    data = await _download(bot, file_id)
    if len(data) > settings.WHISPER_MAX_FILE_BYTES:
        raise _NonRetryable(f"audio too large for API: {len(data)} bytes")

    if message.message_type == "video_note":
        audio = await asyncio.to_thread(_extract_audio_from_video, data)
        filename = "audio.mp3"
    else:
        audio = data
        filename = "audio.ogg"

    text = await _transcribe(audio, filename)

    # Persist transcript + re-run Tier-1 + record cost atomically, so a message
    # never has a transcription without its matching trigger fields.
    async with acquire_connection() as conn, conn.transaction():
        await update_message_transcription(conn, message_id, text)
        result = pattern_cache.match(text, message.sender_role)
        await update_message_triggers(
            conn,
            message_id,
            has_triggers=result.has_triggers,
            base_score=result.base_score,
            triggered_patterns=result.triggered_patterns,
        )
        # Now that the voice note carries text, a high score earns a priority
        # LLM pass just like a typed message would (CLAUDE.md 7.1 step 9).
        if result.base_score >= settings.PRIORITY_SCORE_THRESHOLD:
            await enqueue_task(conn, "priority_llm", {"message_id": str(message_id)})
        await record_whisper_cost(conn, _whisper_cost(duration_s))

    log.info(
        "whisper.transcribed",
        message_id=str(message_id),
        chars=len(text),
        duration_s=duration_s,
        has_triggers=result.has_triggers,
        base_score=result.base_score,
    )


def _media_ref(message: Message) -> tuple[str, int]:
    """Pull the audio ``file_id`` and duration from the stored Telegram payload."""
    raw = message.raw_payload or {}
    media = raw.get(message.message_type)
    if not isinstance(media, dict) or "file_id" not in media:
        raise _NonRetryable(
            f"no {message.message_type} file_id in raw_payload"
        )
    return str(media["file_id"]), int(media.get("duration") or 0)


async def _download(bot: Bot, file_id: str) -> bytes:
    """Fetch a Telegram file's bytes via get_file + download_file."""
    tg_file = await bot.get_file(file_id)
    if tg_file.file_path is None:
        raise _NonRetryable("telegram returned no file_path")
    buf = await bot.download_file(tg_file.file_path)
    if buf is None:
        raise RuntimeError("download_file returned no buffer")
    return buf.read()


def _extract_audio_from_video(data: bytes) -> bytes:
    """ffmpeg: strip the audio track from a video_note .mp4 to mono 16 kHz mp3.

    Synchronous on purpose (run via ``asyncio.to_thread``): subprocess + temp
    file I/O stay off the event loop. The input goes through a temp file because
    .mp4 needs a seekable source (the moov atom may sit at the end); the audio
    comes back on stdout.
    """
    if shutil.which("ffmpeg") is None:
        raise _NonRetryable("ffmpeg not available on this host")
    with tempfile.NamedTemporaryFile(suffix=".mp4", delete=False) as tf:
        tf.write(data)
        in_path = tf.name
    try:
        proc = subprocess.run(
            ["ffmpeg", "-i", in_path, "-vn", "-ac", "1", "-ar", "16000",
             "-f", "mp3", "pipe:1"],
            capture_output=True,
            check=False,
        )
        if proc.returncode != 0:
            tail = proc.stderr.decode("utf-8", "ignore")[-500:]
            raise RuntimeError(f"ffmpeg exit {proc.returncode}: {tail}")
        return proc.stdout
    finally:
        with contextlib.suppress(OSError):
            os.unlink(in_path)


async def _transcribe(audio: bytes, filename: str) -> str:
    """Call the OpenAI Audio API and return the transcript text."""
    client = _get_client()
    resp = await client.audio.transcriptions.create(
        model=settings.WHISPER_MODEL,
        file=(filename, audio),
    )
    return resp.text


def _whisper_cost(duration_seconds: int) -> Decimal:
    """Estimated USD cost for ``duration_seconds`` of audio (whisper-1 pricing)."""
    cost = _WHISPER_USD_PER_MINUTE * Decimal(duration_seconds) / Decimal(60)
    return cost.quantize(Decimal("0.0001"), rounding=ROUND_HALF_UP)
