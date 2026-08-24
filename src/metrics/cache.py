"""Tiny in-process TTL cache for the preview-stand pages.

Exists for exactly one reason: switching between the stand's two modes means
re-requesting pages that are expensive to assemble (the team summary re-collects
120 days of metrics; the risk copy re-reads two ~250 KB stored reports), and a
review surface does not need second-fresh data. A few minutes of staleness is
invisible to a human toggling views; the toggle becoming instant is not.

Deliberately NOT used by any production route — the live report keeps its
existing behaviour, and correctness surfaces must never trade freshness for
speed silently. ``?fresh=1`` on the stand bypasses the cache when needed.
"""

from __future__ import annotations

import time


class TTLCache:
    """String cache with a fixed time-to-live. Process-local, not thread-safe
    beyond asyncio's single-threaded event loop — which is where it lives."""

    def __init__(self, ttl_seconds: float) -> None:
        self._ttl = ttl_seconds
        self._store: dict[str, tuple[float, str]] = {}

    def get(self, key: str) -> str | None:
        hit = self._store.get(key)
        if hit is None:
            return None
        expires_at, value = hit
        if time.monotonic() >= expires_at:
            self._store.pop(key, None)
            return None
        return value

    def put(self, key: str, value: str) -> None:
        self._store[key] = (time.monotonic() + self._ttl, value)

    def clear(self) -> None:
        self._store.clear()


#: Shared by both stand pages. Three minutes: long enough that flipping between
#: modes never recomputes, short enough that a reviewer refreshing after a real
#: data change sees it without thinking about caches.
preview_cache = TTLCache(ttl_seconds=180)
