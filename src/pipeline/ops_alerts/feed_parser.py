"""Parse + filter the payment-provider RSS feed into Incident records.

Pure parsing (``parse_incidents``) is separated from the network fetch
(``fetch_incidents``) so the filtering logic is unit-testable without IO.

Divergence from the original spec, on purpose: ``parse_incidents`` filters by
country + provider only, NOT by status. The spec both (a) filtered out
Resolved/Completed and (b) expected the update flow to edit a message to
"RESOLVED" — mutually exclusive. We keep resolved incidents in the parse output
and let the worker decide: a resolution edits an already-posted message; a
resolved incident we never announced is ignored.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import UTC, datetime
from time import struct_time
from typing import Any

import feedparser
import httpx
from tenacity import retry, stop_after_attempt, wait_fixed

from src.utils.logging import get_logger

log = get_logger(__name__)

# Statuses that mean the incident is over. Used by the worker (not the parser).
RESOLVED_STATUSES = {"Resolved", "Completed"}

# Target providers per country. A feed provider matches if any target is a
# case-sensitive substring of it (spec: provider.includes(target)).
TARGET_PROVIDERS: dict[str, list[str]] = {
    "Chile": [
        "Webpay", "Banco Estado", "BCI", "Falabella", "Santander",
        "Itaú", "MercadoPago", "Bank Transfer", "Banco de Chile", "Scotiabank",
    ],
    "Argentina": [
        "BBVA", "Galicia", "Mercado Pago",
    ],
}

# The feed's <summary> is HTML; each update is `<strong>Status</strong> - text`,
# newest first. The latest status is the first <strong> tag.
_STATUS_RE = re.compile(r"<strong>\s*([A-Za-z ]+?)\s*</strong>")


@dataclass(frozen=True)
class Incident:
    incident_id: str
    country: str
    provider: str
    issue: str | None
    link: str | None
    details: str | None
    status: str
    iso_date: datetime | None

    @property
    def is_resolved(self) -> bool:
        return self.status in RESOLVED_STATUSES


def extract_latest_status(text: str | None) -> str:
    """Pull the most recent status out of the feed summary HTML."""
    m = _STATUS_RE.search(text or "")
    if m:
        s = m.group(1).strip().lower()
        if s == "in progress":
            return "In progress"
        return s[0].upper() + s[1:] if s else "Unknown"
    return "Unknown"


def _parse_title(title: str | None) -> tuple[str, str, str | None] | None:
    """Parse 'Country - Provider - Issue' → (country, provider, issue) or None."""
    if not title:
        return None
    parts = title.split(" - ")
    if len(parts) < 2:
        return None
    country = parts[0].strip()
    provider = parts[1].strip()
    issue = parts[2].strip() if len(parts) > 2 else None
    return country, provider, issue


def _matches_target(country: str, provider: str) -> bool:
    targets = TARGET_PROVIDERS.get(country)
    if not targets:
        return False
    return any(t in provider for t in targets)


def _incident_id_from_link(link: str | None) -> str:
    return link.split("/")[-1] if link else ""


def _to_datetime(st: struct_time | None) -> datetime | None:
    if st is None:
        return None
    return datetime(*st[:6], tzinfo=UTC)


def parse_incidents(entries: list[Any]) -> list[Incident]:
    """Filter feed entries to country+provider matches, as Incident records.

    Each entry is a feedparser-style mapping exposing ``.get(key)`` for
    ``title`` / ``link`` / ``summary`` and ``published_parsed`` /
    ``updated_parsed`` (``time.struct_time``). Status filtering is NOT applied
    here (see module docstring).
    """
    out: list[Incident] = []
    for entry in entries:
        title = entry.get("title")
        parsed = _parse_title(title)
        if parsed is None:
            continue
        country, provider, issue = parsed
        if not _matches_target(country, provider):
            continue
        link = entry.get("link")
        incident_id = _incident_id_from_link(link)
        if not incident_id:
            continue
        snippet = entry.get("summary")
        status = extract_latest_status(snippet)
        iso = _to_datetime(entry.get("updated_parsed") or entry.get("published_parsed"))
        out.append(
            Incident(
                incident_id=incident_id,
                country=country,
                provider=provider,
                issue=issue,
                link=link,
                details=snippet,
                status=status,
                iso_date=iso,
            )
        )
    return out


async def fetch_incidents(
    url: str, *, retries: int, retry_delay_seconds: int
) -> list[Incident]:
    """Fetch + parse the feed. Retries on transient HTTP errors; raises if all fail."""

    @retry(stop=stop_after_attempt(retries), wait=wait_fixed(retry_delay_seconds))
    async def _get() -> str:
        async with httpx.AsyncClient(timeout=20.0, follow_redirects=True) as client:
            resp = await client.get(url)
            resp.raise_for_status()
            return resp.text

    xml = await _get()
    parsed = feedparser.parse(xml)
    return parse_incidents(list(parsed.entries))
