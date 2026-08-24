"""Parser for Telegram Desktop HTML exports (``messages*.html``).

The export is machine-generated and structurally stable, so it is parsed with
targeted regexes rather than a full HTML stack (no bs4/lxml dependency).

Shape of one export folder, keyed by aff_id::

    AFFS_CHATS/<aff_id>/
        messages.html, messages2.html, …   paginated history, ~1000 msgs per file
        photos/                            downloaded photos
        files/                             downloaded documents
        css/, js/, images/                 export UI chrome — ignored

What the format DOES carry:
  * ``id="messageN"``  → the real per-chat Telegram message id (range seen: 1…58779)
  * ``title="DD.MM.YYYY HH:MM:SS UTC±HH:MM"`` → full timestamp, every message
  * ``from_name``      → sender *display name* only
  * reply target via ``GoToMessage(N)``, forward origin, media links, reactions

What it does NOT carry — and why imported rows are second-class:
  * no Telegram ``chat_id``  → an unmatched export cannot be bound to a real chat
  * no sender ``user_id``    → ``messages.sender_id`` stays NULL, so imported rows
    can never be identity-joined to ``internal_users`` the way live rows are.
    ``sender_role`` is therefore a *heuristic* (see ``classify_sender_role``).

Two classes of ``message service`` block exist and must not be conflated:
  * negative id (``id="message-1"``) → a date divider ("19 February 2026"); pure
    presentation, never a message. 7 610 of these across the archive.
  * positive id → a real chat event ("X invited Y", "pinned this message",
    "changed group photo"). 1 017 of these; they belong in ``chat_events``,
    not ``messages``.
"""

from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass
from datetime import datetime
from html import unescape
from pathlib import Path

# --- message / event block boundaries ---------------------------------------
_RE_BLOCK_START = re.compile(
    r'<div class="message (default clearfix(?: joined)?|service)" id="message(-?\d+)">'
)
_RE_DATE = re.compile(
    r'<div class="pull_right date details" title="'
    r'(\d{2})\.(\d{2})\.(\d{4}) (\d{2}):(\d{2}):(\d{2}) UTC([+-])(\d{2}):(\d{2})"'
)
_RE_FROM_NAME = re.compile(r'<div class="from_name">\s*(.*?)\s*</div>', re.S)
# Same-file replies render as GoToMessage(N); replies whose target lives in
# another page of the export render only as an anchor (22 across the archive).
_RE_REPLY_TO = re.compile(r"GoToMessage\((\d+)\)|#go_to_message(\d+)")
_RE_TEXT = re.compile(r'<div class="text">\s*(.*?)\s*</div>', re.S)
_RE_FORWARDED = re.compile(r'<div class="forwarded body">(.*?)(?=<div class="text">|\Z)', re.S)
_RE_SERVICE_BODY = re.compile(r'<div class="body details">\s*(.*?)\s*</div>', re.S)
# A forward header nests the original send time inside the name div:
#   <div class="from_name">Mirror | Betonwin<span class="date details" …> 19.01.2026 …</span>
# Strip that span, or the date ends up glued onto the origin name.
_RE_INLINE_DATE = re.compile(r'<span class="date details".*?</span>', re.S)
_RE_REACTIONS = re.compile(r'<span class="reactions">.*?</span>\s*(?=</div>)', re.S)

# --- media ------------------------------------------------------------------
_MEDIA_DIRS = "photos|files|video_files|voice_messages|round_video_messages"
_RE_MEDIA_HREF = re.compile(rf'href="((?:{_MEDIA_DIRS})/[^"]+)"')
_RE_MEDIA_KIND = re.compile(r'class="media clearfix pull_left (?:block_link )?media_([a-z_]+)"')
_RE_MEDIA_TITLE = re.compile(r'<div class="title bold">\s*(.*?)\s*</div>', re.S)
_RE_NOT_INCLUDED = re.compile(r"change data exporting settings to download")
_RE_PHOTO_WRAP = re.compile(r'class="photo_wrap[^"]*" href="([^"]+)"')

# --- text post-processing ---------------------------------------------------
_RE_BR = re.compile(r"<br\s*/?>", re.I)
_RE_ANCHOR = re.compile(r'<a\s+href="([^"]*)"[^>]*>(.*?)</a>', re.S | re.I)
_RE_TAG = re.compile(r"<[^>]+>")
_RE_LINK = re.compile(r"https?://[^\s<>\"')]+", re.I)
_RE_MENTION = re.compile(r"@([A-Za-z0-9_]{4,32})")
_RE_AFF_ID = re.compile(r"\b(\d{4,6})\b")
_RE_CHAT_TITLE = re.compile(r'<div class="text bold">\s*(.*?)\s*</div>', re.S)

# Display-name suffix marking an internal staff member. Every internal account in
# the archive carries a brand tag in its Telegram name — "Mirror | Betonwin",
# "Kowalski | BetonWin", "Сhicco| Betonwin" (note the missing space, and the
# Cyrillic 'С' homoglyph) — while partner-side contacts do not. This is the only
# signal available: the export has no user ids.
_INTERNAL_MARKERS = ("betonwin", "beton.win", "beton win")

# Telegram export writes these display names for accounts it cannot resolve.
_ANONYMOUS_NAMES = {"deleted account", "deleted", ""}

#: Media-folder name → ``messages.message_type``, matching ``ingest._classify_type``.
_MEDIA_TYPE = {
    "photo": "photo",
    "video": "video",
    "voice_message": "voice",
    "round_video_message": "video_note",
    "file": "document",
    "audio_file": "audio",
    "animation": "animation",
    "sticker": "sticker",
    "poll": "poll",
    "location": "location",
    "contact": "contact",
    "call": "other",
    "game": "other",
    "shop": "other",
    "music": "audio",
}


@dataclass(frozen=True)
class ParsedMessage:
    """One real message from the export, ready to map onto a ``messages`` row."""

    telegram_message_id: int
    timestamp: datetime
    sender_name: str | None
    sender_role: str
    text: str | None
    message_type: str
    reply_to_message_id: int | None
    #: True for any forward. 50 of the 248 forwards in the archive come from a
    #: hidden origin and carry no name, so the flag and the name are separate.
    is_forward: bool
    forward_from_name: str | None
    #: Export-relative paths (``photos/x.jpg``) that actually exist on disk.
    media_paths: tuple[str, ...]
    #: True when the export references media it did not download.
    media_omitted: bool
    links: tuple[str, ...]
    mentions: tuple[str, ...]


@dataclass(frozen=True)
class ParsedEvent:
    """A positive-id ``message service`` block → a ``chat_events`` row."""

    telegram_message_id: int
    text: str


@dataclass(frozen=True)
class ParsedExport:
    """Everything one aff_id folder yields."""

    aff_id: str
    chat_title: str | None
    #: Every 4–6 digit id in the chat title, folder id first. A single chat often
    #: covers several aff_ids ("LEGENDS | Betonwin | 58329 | 71862 | 74849").
    aff_ids: tuple[str, ...]
    messages: tuple[ParsedMessage, ...]
    events: tuple[ParsedEvent, ...]
    #: Stable digest of the message stream, used to spot the same chat exported
    #: under two aff_ids (77106 and 78284 are byte-identical histories).
    content_hash: str
    source_files: tuple[str, ...]

    @property
    def first_timestamp(self) -> datetime | None:
        return self.messages[0].timestamp if self.messages else None

    @property
    def last_timestamp(self) -> datetime | None:
        return self.messages[-1].timestamp if self.messages else None


def classify_sender_role(sender_name: str | None) -> str:
    """Best-effort ``messages.sender_role`` from a display name alone.

    The export carries no user ids, so role is inferred from the brand tag that
    internal accounts keep in their Telegram display name. Returns one of
    ``internal`` / ``partner`` / ``anonymous_admin`` / ``unknown`` to match the
    vocabulary live ingestion writes.
    """
    if sender_name is None:
        return "unknown"
    normalised = sender_name.strip().lower()
    if normalised in _ANONYMOUS_NAMES:
        return "anonymous_admin"
    if any(marker in normalised for marker in _INTERNAL_MARKERS):
        return "internal"
    return "partner"


def _clean_text(raw: str) -> tuple[str | None, tuple[str, ...]]:
    """HTML fragment → (plain text, hrefs). ``<br>`` becomes a newline."""
    hrefs: list[str] = []

    def _keep_href(match: re.Match[str]) -> str:
        href, label = match.group(1), match.group(2)
        if href:
            hrefs.append(unescape(href))
        return label

    text = _RE_BR.sub("\n", raw)
    text = _RE_ANCHOR.sub(_keep_href, text)
    text = _RE_TAG.sub("", text)
    text = unescape(text).strip()
    # Bare URLs that were never wrapped in an anchor.
    for url in _RE_LINK.findall(text):
        if url not in hrefs:
            hrefs.append(url)
    return (text or None), tuple(dict.fromkeys(hrefs))


def _reply_target(block: str) -> int | None:
    """Replied-to message id, from either the JS call or the bare anchor form."""
    match = _RE_REPLY_TO.search(block)
    if match is None:
        return None
    return int(match.group(1) or match.group(2))


def _parse_timestamp(block: str) -> datetime | None:
    match = _RE_DATE.search(block)
    if match is None:
        return None
    day, month, year, hour, minute, second, sign, off_h, off_m = match.groups()
    offset = f"{sign}{off_h}:{off_m}"
    return datetime.fromisoformat(
        f"{year}-{month}-{day}T{hour}:{minute}:{second}{offset}"
    )


def _existing_media(folder: Path, block: str) -> tuple[tuple[str, ...], bool]:
    """Media paths that are present on disk, plus whether any were omitted."""
    candidates = list(_RE_MEDIA_HREF.findall(block)) + list(_RE_PHOTO_WRAP.findall(block))
    present: list[str] = []
    for rel in dict.fromkeys(candidates):
        rel_clean = unescape(rel)
        # Thumbnails duplicate the full-size file; keep only the original.
        if "_thumb." in rel_clean:
            continue
        if (folder / rel_clean).is_file():
            present.append(rel_clean)
    omitted = bool(_RE_NOT_INCLUDED.search(block))
    return tuple(present), omitted


def _classify(block: str, text: str | None, media_paths: tuple[str, ...]) -> str:
    """Coarse ``message_type``; text wins over media, mirroring live ingestion."""
    if text:
        return "text"
    kind_match = _RE_MEDIA_KIND.search(block)
    if kind_match is not None:
        return _MEDIA_TYPE.get(kind_match.group(1), "other")
    if media_paths:
        first = media_paths[0]
        if first.startswith("photos/"):
            return "photo"
        if first.startswith("voice_messages/"):
            return "voice"
        if first.startswith("round_video_messages/"):
            return "video_note"
        if first.startswith("video_files/"):
            return "video"
        return "document"
    if _RE_PHOTO_WRAP.search(block) is not None:
        return "photo"
    if _RE_MEDIA_TITLE.search(block) is not None or _RE_NOT_INCLUDED.search(block):
        return "media"
    return "other"


def _iter_blocks(html: str) -> list[tuple[str, int, str]]:
    """Split one ``messages*.html`` into (kind, message_id, block) triples."""
    starts = list(_RE_BLOCK_START.finditer(html))
    blocks: list[tuple[str, int, str]] = []
    for index, match in enumerate(starts):
        end = starts[index + 1].start() if index + 1 < len(starts) else len(html)
        blocks.append((match.group(1), int(match.group(2)), html[match.start() : end]))
    return blocks


def parse_export_folder(folder: Path) -> ParsedExport:
    """Parse one ``AFFS_CHATS/<aff_id>/`` folder into a :class:`ParsedExport`."""
    aff_id = folder.name
    # messages.html, messages2.html, … — numeric order, not lexicographic
    # ("messages10.html" must not sort before "messages2.html").
    files = sorted(
        folder.glob("messages*.html"),
        key=lambda p: int(re.sub(r"\D", "", p.stem) or "1"),
    )

    chat_title: str | None = None
    messages: list[ParsedMessage] = []
    events: list[ParsedEvent] = []
    digest = hashlib.sha256()
    last_sender: str | None = None

    for path in files:
        html = path.read_text(encoding="utf-8", errors="replace")
        if chat_title is None:
            title_match = _RE_CHAT_TITLE.search(html)
            if title_match is not None:
                chat_title, _ = _clean_text(title_match.group(1))

        for kind, message_id, block in _iter_blocks(html):
            if kind == "service":
                # Negative ids are date dividers — presentation only.
                if message_id < 0:
                    continue
                body = _RE_SERVICE_BODY.search(block)
                if body is not None:
                    event_text, _ = _clean_text(body.group(1))
                    if event_text:
                        events.append(ParsedEvent(message_id, event_text))
                continue

            timestamp = _parse_timestamp(block)
            if timestamp is None:
                # No usable time → cannot place it on the analysis timeline.
                continue

            # Reactions carry other users' display names in `title=` attributes;
            # drop them before any text/from_name extraction.
            body_block = _RE_REACTIONS.sub("", block)

            # A forward's own header also contains a `from_name`, so pull the
            # forwarded sub-block out before reading the message's sender.
            forward_match = _RE_FORWARDED.search(body_block)
            forward_from: str | None = None
            if forward_match is not None:
                forward_block = forward_match.group(1)
                body_block = body_block.replace(forward_block, "")
                fwd_name = _RE_FROM_NAME.search(forward_block)
                if fwd_name is not None:
                    forward_from, _ = _clean_text(_RE_INLINE_DATE.sub("", fwd_name.group(1)))

            name_match = _RE_FROM_NAME.search(body_block)
            if name_match is not None:
                sender_name, _ = _clean_text(name_match.group(1))
                last_sender = sender_name
            else:
                # "joined" blocks omit the name and continue the previous sender.
                sender_name = last_sender

            text_match = _RE_TEXT.search(body_block)
            text, links = _clean_text(text_match.group(1)) if text_match else (None, ())
            media_paths, media_omitted = _existing_media(folder, block)

            messages.append(
                ParsedMessage(
                    telegram_message_id=message_id,
                    timestamp=timestamp,
                    sender_name=sender_name,
                    sender_role=classify_sender_role(sender_name),
                    text=text,
                    message_type=_classify(block, text, media_paths),
                    reply_to_message_id=_reply_target(body_block),
                    is_forward=forward_match is not None,
                    forward_from_name=forward_from,
                    media_paths=media_paths,
                    media_omitted=media_omitted,
                    links=links,
                    mentions=tuple(dict.fromkeys(_RE_MENTION.findall(text or ""))),
                )
            )
            digest.update(f"{message_id}\x1f{timestamp.isoformat()}\x1f{text or ''}\x1e".encode())

    messages.sort(key=lambda m: (m.timestamp, m.telegram_message_id))
    ids = [aff_id] + [i for i in _RE_AFF_ID.findall(chat_title or "") if i != aff_id]

    return ParsedExport(
        aff_id=aff_id,
        chat_title=chat_title,
        aff_ids=tuple(dict.fromkeys(ids)),
        messages=tuple(messages),
        events=tuple(events),
        content_hash=digest.hexdigest(),
        source_files=tuple(p.name for p in files),
    )


def parse_archive(root: Path) -> list[ParsedExport]:
    """Parse every ``<aff_id>/`` folder under *root*, skipping export cruft."""
    exports: list[ParsedExport] = []
    for folder in sorted(root.iterdir()):
        if not folder.is_dir() or folder.name.startswith((".", "__")):
            continue
        exports.append(parse_export_folder(folder))
    return exports
