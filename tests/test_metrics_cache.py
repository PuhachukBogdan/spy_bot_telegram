"""The preview stand's TTL cache. Time is monkeypatched — no sleeping in tests."""

from __future__ import annotations

import pytest

from src.metrics import cache as cache_module
from src.metrics.cache import TTLCache


def test_hit_within_ttl(monkeypatch: pytest.MonkeyPatch) -> None:
    now = 1000.0
    monkeypatch.setattr(cache_module.time, "monotonic", lambda: now)
    c = TTLCache(ttl_seconds=180)
    c.put("summary", "<html>")
    assert c.get("summary") == "<html>"


def test_expires_after_ttl(monkeypatch: pytest.MonkeyPatch) -> None:
    now = 1000.0
    monkeypatch.setattr(cache_module.time, "monotonic", lambda: now)
    c = TTLCache(ttl_seconds=180)
    c.put("summary", "<html>")

    now = 1179.9
    assert c.get("summary") == "<html>"
    now = 1180.0  # exactly at expiry — gone, and the entry is dropped
    assert c.get("summary") is None
    assert c.get("summary") is None  # second read after eviction stays None


def test_keys_are_independent(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(cache_module.time, "monotonic", lambda: 0.0)
    c = TTLCache(ttl_seconds=60)
    c.put("summary:30", "a")
    c.put("risk:today", "b")
    assert c.get("summary:30") == "a"
    assert c.get("risk:today") == "b"
    assert c.get("risk:2026-08-14") is None


def test_put_overwrites_and_refreshes(monkeypatch: pytest.MonkeyPatch) -> None:
    now = 0.0
    monkeypatch.setattr(cache_module.time, "monotonic", lambda: now)
    c = TTLCache(ttl_seconds=60)
    c.put("k", "old")
    now = 50.0
    c.put("k", "new")  # refreshed at t=50 → alive until t=110
    now = 100.0
    assert c.get("k") == "new"


def test_clear(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(cache_module.time, "monotonic", lambda: 0.0)
    c = TTLCache(ttl_seconds=60)
    c.put("k", "v")
    c.clear()
    assert c.get("k") is None
