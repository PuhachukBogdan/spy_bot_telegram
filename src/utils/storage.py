"""Best-effort Supabase Storage uploads (LLM audit blobs). Phase 8.

The LLM audit trail archives the full prompt and response to Storage so the
``llm_calls`` row can stay small (it keeps only a hash, a summary, and the blob
paths). Uploads are BEST-EFFORT: a Storage failure must never break the LLM
pipeline, so :func:`upload_text` swallows errors and returns ``False`` — the
caller then stores a NULL path and carries on.

Uses the Storage REST API directly via httpx (already a dependency) with the
service key, so no extra client lifecycle is introduced.
"""

from __future__ import annotations

import httpx

from src.config import settings
from src.utils.logging import get_logger

log = get_logger(__name__)

_UPLOAD_TIMEOUT_S = 10.0


async def upload_text(
    path: str, text: str, *, content_type: str = "text/plain; charset=utf-8"
) -> bool:
    """Upsert ``text`` to ``<bucket>/<path>`` in Supabase Storage.

    Returns ``True`` on success, ``False`` on any HTTP/transport failure (logged,
    never raised).
    """
    base = settings.SUPABASE_URL.rstrip("/")
    bucket = settings.SUPABASE_STORAGE_BUCKET
    url = f"{base}/storage/v1/object/{bucket}/{path}"
    key = settings.SUPABASE_SERVICE_KEY.get_secret_value()
    headers = {
        "Authorization": f"Bearer {key}",
        "apikey": key,
        "Content-Type": content_type,
        "x-upsert": "true",
    }
    try:
        async with httpx.AsyncClient(timeout=_UPLOAD_TIMEOUT_S) as client:
            resp = await client.post(url, content=text.encode("utf-8"), headers=headers)
        if resp.status_code >= 300:
            log.warning("storage.upload_failed", path=path, status=resp.status_code)
            return False
        return True
    except httpx.HTTPError as exc:
        log.warning("storage.upload_error", path=path, error=str(exc))
        return False
