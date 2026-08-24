"""Uploading the archive's photos and documents to Supabase Storage.

Runs as a separate phase after the messages are in, so a Storage outage never
blocks or rolls back the history import — the rows are already correct, they just
lack a ``storage_key`` until this is re-run.

Only files that exist on disk are uploaded. The export references media it was
never asked to download ("Not included, change data exporting settings to
download." — 231 such messages), and it also ships ~10 000 identical UI icons under
``images/`` in every folder, which are export chrome and not chat content at all.
Both are skipped: the archive's real payload is 895 files, ~54 MB.
"""

from __future__ import annotations

import asyncio
import mimetypes
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any
from uuid import UUID

import asyncpg

from src.utils.logging import get_logger
from src.utils.storage import upload_bytes

log = get_logger(__name__)

#: Storage key prefix, so archive objects never mix with LLM audit blobs.
_PREFIX = "archive"

#: Concurrent uploads. Matches the ops-alerts fan-out convention (bounded, modest)
#: rather than saturating the link from a laptop.
_CONCURRENCY = 8

#: Refuse absurdly large members — a guard against a malformed export, not a policy.
_MAX_BYTES = 25 * 1024 * 1024


@dataclass
class MediaResult:
    uploaded: int = 0
    skipped_existing: int = 0
    missing: int = 0
    too_large: int = 0
    failed: int = 0
    bytes_sent: int = 0
    errors: list[str] = field(default_factory=list)


#: Characters Supabase Storage accepts in an object key, beyond the path separator.
#: Anything else is replaced. Telegram exports keep the original filename, which can
#: contain brackets and other punctuation — `Final [CASINO] Wewe IO v2025.docx` was
#: rejected with a 400 until this existed. The unmodified path stays in the message's
#: ``raw_payload``, so nothing is lost by sanitising the key.
_UNSAFE_KEY_CHARS = re.compile(r"[^A-Za-z0-9._\-/]+")


def storage_key(aff_id: str, relative_path: str) -> str:
    """``archive/<aff_id>/<sanitised path>`` — stable, readable, and accepted by Storage."""
    safe = _UNSAFE_KEY_CHARS.sub("_", relative_path).strip("_")
    return f"{_PREFIX}/{aff_id}/{safe}"


@dataclass(frozen=True)
class _Pending:
    message_id: UUID
    aff_id: str
    relative_path: str
    absolute_path: Path
    payload: dict[str, Any]


async def _collect(conn: asyncpg.Connection, root: Path) -> list[_Pending]:
    """Imported messages whose provenance lists media without a ``storage_key``."""
    rows = await conn.fetch(
        """
        SELECT id, raw_payload
        FROM messages
        WHERE source = 'imported'
          AND raw_payload -> 'import' -> 'media' IS NOT NULL
        """
    )
    pending: list[_Pending] = []
    for row in rows:
        # One payload dict per message, SHARED by every _Pending derived from it. That
        # sharing is load-bearing: a message with several attachments produces several
        # pending items, and each `_with_key` call has to accumulate onto the same
        # object so the final UPDATE carries all of the keys rather than the last one.
        payload = dict(row["raw_payload"])
        detail = payload.get("import", {})
        aff_id = str(detail.get("aff_id"))
        for item in detail.get("media", []):
            if item.get("storage_key"):
                continue
            relative = str(item["path"])
            pending.append(
                _Pending(
                    message_id=row["id"],
                    aff_id=aff_id,
                    relative_path=relative,
                    absolute_path=root / aff_id / relative,
                    payload=payload,
                )
            )
    return pending


async def _upload_one(item: _Pending, result: MediaResult) -> str | None:
    """Upload one file; return its storage key, or ``None`` if it was not sent."""
    if not item.absolute_path.is_file():
        result.missing += 1
        return None
    size = item.absolute_path.stat().st_size
    if size > _MAX_BYTES:
        result.too_large += 1
        result.errors.append(f"{item.aff_id}/{item.relative_path}: {size} bytes")
        return None

    key = storage_key(item.aff_id, item.relative_path)
    content_type, _ = mimetypes.guess_type(item.relative_path)
    data = item.absolute_path.read_bytes()
    ok = await upload_bytes(
        key, data, content_type=content_type or "application/octet-stream"
    )
    if not ok:
        result.failed += 1
        result.errors.append(f"{item.aff_id}/{item.relative_path}: upload failed")
        return None
    result.uploaded += 1
    result.bytes_sent += size
    return key


def _with_key(payload: dict[str, Any], relative_path: str, key: str) -> dict[str, Any]:
    """Return *payload* with ``storage_key`` recorded against one media entry."""
    detail = payload.setdefault("import", {})
    for item in detail.get("media", []):
        if item.get("path") == relative_path:
            item["storage_key"] = key
    return payload


async def upload_archive_media(
    pool_acquire: Any, root: Path, *, dry_run: bool = False
) -> MediaResult:
    """Upload every not-yet-uploaded archive file and record its key on the message.

    Idempotent: an entry that already has a ``storage_key`` is never re-uploaded, so
    a partial run is safe to repeat.
    """
    result = MediaResult()

    async with pool_acquire() as conn:
        pending = await _collect(conn, root)

    log.info("archive.media_pending", count=len(pending))
    if dry_run:
        result.skipped_existing = len(pending)
        return result

    semaphore = asyncio.Semaphore(_CONCURRENCY)
    # raw_payload is per-message but a message can carry several files, so keys are
    # accumulated per message and written once at the end.
    updates: dict[UUID, dict[str, Any]] = {}
    lock = asyncio.Lock()

    async def _worker(item: _Pending) -> None:
        async with semaphore:
            key = await _upload_one(item, result)
        if key is None:
            return
        async with lock:
            payload = updates.get(item.message_id, item.payload)
            updates[item.message_id] = _with_key(payload, item.relative_path, key)

    await asyncio.gather(*(_worker(item) for item in pending))

    if updates:
        async with pool_acquire() as conn:
            await conn.executemany(
                "UPDATE messages SET raw_payload = $2 WHERE id = $1",
                list(updates.items()),
            )

    log.info(
        "archive.media_done",
        uploaded=result.uploaded,
        failed=result.failed,
        missing=result.missing,
        mb=round(result.bytes_sent / 1024 / 1024, 1),
    )
    return result
