"""Tone of voice — the pure half. No I/O, no LLM call, no rendering.

What lives here:

* the **metric registry** (:data:`TONE_METRICS`) — key, label, polarity — the one
  place that says what each dimension means for a number on the page;
* the **conversation renderer** the daily pass sends to the model;
* the **windowing** of a long day into model-sized chunks;
* the **acceptance gate** applied to what the model returns.

The gate is the point. The pass is meant to be lenient, and leniency is enforced
here rather than hoped for from the prompt: a flag survives only if its confidence
clears the floor, it points at a message the model was actually asked to assess,
and its quote really occurs in that message. A model under pressure to find
something will hedge and paraphrase; both are rejected mechanically, the same way
the archive retro pass rejected unanchored findings.

Everything is per-message and additive, so a period's rate is always
``flagged / assessed`` over summed day counters — never an average of averages.
"""

from __future__ import annotations

import re
from collections.abc import Iterable, Sequence
from dataclasses import dataclass, field
from datetime import UTC, date, datetime, time, tzinfo
from enum import StrEnum
from html import escape
from typing import Any

from src.db.models import Message
from src.llm.tone_schema import ToneFlag, ToneMetric


class TonePolarity(StrEnum):
    """How a flag count turns into a number on the page."""

    #: A flag is bad; the page shows the flag RATE and the ideal is 0 %.
    NEGATIVE = "negative"
    #: A flag is a gap in something expected; the page shows 100 − rate, ideal 100 %.
    POSITIVE_GAP = "positive_gap"
    #: A flag is a bad thing that happened; the page shows the COUNT (and per 100),
    #: ideal 0. Chosen (2026-09-11) for rare events whose denominator is "all
    #: messages": as 100 − rate they sat at 99.x % and read as a grade.
    NEGATIVE_EVENT = "negative_event"
    #: A flag is a good thing that happened; the page shows the COUNT (and per 100).
    POSITIVE_EVENT = "positive_event"


@dataclass(frozen=True)
class ToneMetricDef:
    key: ToneMetric
    label: str
    polarity: TonePolarity
    description: str

    def to_payload(self) -> dict[str, str]:
        return {
            "key": self.key.value,
            "label": self.label,
            "polarity": self.polarity.value,
            "description": self.description,
        }


#: Display order on the page. Keys are what the DB stores.
TONE_METRICS: tuple[ToneMetricDef, ...] = (
    # Descriptions double as the gauge tooltips. Each one names the denominator
    # explicitly — ALL judged manager messages, not "complaints" or "questions" —
    # because "1 of 255" under Handling complaints was read as 255 complaints.
    ToneMetricDef(
        ToneMetric.TOXICITY,
        "Toxicity",
        TonePolarity.NEGATIVE,
        "Rudeness, contempt or aggression toward a partner. "
        "Flagged messages ÷ all judged messages; ideal 0 %.",
    ),
    ToneMetricDef(
        ToneMetric.COMPLETENESS,
        "Completeness",
        TonePolarity.POSITIVE_GAP,
        "A reply that plainly leaves an asked part unanswered. "
        "100 − (gaps ÷ all judged messages); 100 % = no gaps found.",
    ),
    ToneMetricDef(
        ToneMetric.COURTESY,
        "Courtesy & register",
        TonePolarity.POSITIVE_GAP,
        "Register clearly off for the relationship. "
        "100 − (cases ÷ all judged messages); 100 % = nothing a colleague would wince at.",
    ),
    ToneMetricDef(
        ToneMetric.DEESCALATION,
        "Handling complaints",
        TonePolarity.NEGATIVE_EVENT,
        "An unhappy partner met with no acknowledgement and no step. "
        "A count of clear misses in the period (plus per 100 judged messages); ideal 0.",
    ),
    ToneMetricDef(
        ToneMetric.INITIATIVE,
        "Initiative",
        TonePolarity.POSITIVE_EVENT,
        "Heads-ups, suggestions and help the partner did not ask for. "
        "A count of good moves, plus the rate per 100 judged messages.",
    ),
)

METRIC_BY_KEY: dict[str, ToneMetricDef] = {m.key.value: m for m in TONE_METRICS}


def metric_defs_payload() -> list[dict[str, str]]:
    return [m.to_payload() for m in TONE_METRICS]


# ---------------------------------------------------------------------------
# Day bounds
# ---------------------------------------------------------------------------


def local_day_bounds(day: date, tz: tzinfo) -> tuple[datetime, datetime]:
    """``[start, end)`` of a local calendar day, as UTC instants.

    Built on the local wall clock and converted afterwards, so a day across a
    DST change is 23 or 25 hours long rather than a shifted 24.
    """
    start = datetime.combine(day, time(0), tzinfo=tz)
    end = datetime.combine(day + _ONE_DAY, time(0), tzinfo=tz)
    return start.astimezone(UTC), end.astimezone(UTC)


_ONE_DAY = date.resolution


# ---------------------------------------------------------------------------
# What is assessable
# ---------------------------------------------------------------------------


def message_text(m: Message) -> str:
    """The judgeable text of a message — caption/text, else the transcription."""
    return (m.message_text or m.transcription or "").strip()


def assessable_ids(
    messages: Iterable[Message],
    manager_index: dict[int, Any],
    *,
    start: datetime,
) -> dict[str, Any]:
    """message id -> manager id, for messages the pass should judge.

    A message qualifies when it was written by a real manager (its sender is in
    the manager index), falls inside the day (not the lead-in context), and has
    text to judge. Stickers, bare photos and other empty payloads are context
    only — there is nothing to be rude or incomplete in.
    """
    out: dict[str, Any] = {}
    for m in messages:
        if m.timestamp < start or m.sender_id is None:
            continue
        manager_id = manager_index.get(m.sender_id)
        if manager_id is None or not message_text(m):
            continue
        out[str(m.id)] = manager_id
    return out


# ---------------------------------------------------------------------------
# Conversation block
# ---------------------------------------------------------------------------


def render_tone_block(
    messages: Sequence[Message], assess_ids: Iterable[str], tz: tzinfo
) -> str:
    """Render a window as the injection-safe ``<conversation>`` payload.

    Differs from the risk renderer in three deliberate ways: a stable per-person
    ``speaker`` label (so the model can follow who asked and who answered without
    seeing names), a local ``time`` (so "came back later the same day" is
    visible), and ``assess`` instead of ``flagged`` — only the manager's own
    messages are judged; everything else is read as context.

    Every dynamic value is HTML-escaped: a partner cannot close the tag or plant
    an instruction that escapes the data envelope.
    """
    assess = set(assess_ids)
    speakers: dict[int | None, str] = {}
    lines = ["<conversation>"]
    for m in messages:
        label = speakers.get(m.sender_id)
        if label is None:
            label = f"S{len(speakers) + 1}"
            speakers[m.sender_id] = label
        mid = str(m.id)
        lines.append(
            f'  <message id="{escape(mid, quote=True)}" '
            f'sender_role="{escape(m.sender_role, quote=True)}" '
            f'speaker="{label}" '
            f'time="{m.timestamp.astimezone(tz):%H:%M}" '
            f'assess="{"true" if mid in assess else "false"}">'
            f"{escape(message_text(m))}</message>"
        )
    lines.append("</conversation>")
    lines.append(
        "<assess_messages>[" + ", ".join(sorted(assess)) + "]</assess_messages>"
    )
    return "\n".join(lines)


def plan_windows(
    day_messages: Sequence[Message],
    context: Sequence[Message],
    assessable: dict[str, Any],
    *,
    window_size: int,
    overlap: int,
) -> list[tuple[list[Message], set[str]]]:
    """Split one chat-day into model-sized windows.

    Returns ``(messages_to_render, ids_assessable_in_this_window)`` pairs. The
    first window is preceded by the lead-in ``context`` (the last messages of the
    previous day); later windows carry ``overlap`` messages of their own
    predecessor instead. Overlap and context are rendered but never assessable,
    so a message is judged in exactly one window.
    """
    if not day_messages:
        return []
    windows: list[tuple[list[Message], set[str]]] = []
    step = max(1, window_size)
    for start in range(0, len(day_messages), step):
        own = list(day_messages[start : start + step])
        lead = list(context) if start == 0 else list(day_messages[max(0, start - overlap) : start])
        ids = {str(m.id) for m in own if str(m.id) in assessable}
        windows.append((lead + own, ids))
    return windows


# ---------------------------------------------------------------------------
# Acceptance gate
# ---------------------------------------------------------------------------

_WS = re.compile(r"\s+")
_EDGE_PUNCT = "\"'«»“”‘’.,;:!?…-–— "


def normalize_quote(text: str) -> str:
    """Whitespace-collapsed, lower-cased, edge punctuation stripped."""
    return _WS.sub(" ", text).strip(_EDGE_PUNCT).casefold()


def quote_found(quote: str, text: str) -> bool:
    """Does the model's quote occur in the message, allowing for trivia?

    Case and whitespace differences are forgiven, as are quote marks and
    trailing punctuation the model tends to add or drop. A paraphrase is not.
    """
    q = normalize_quote(quote)
    return bool(q) and q in normalize_quote(text)


@dataclass
class ToneGateStats:
    accepted: int = 0
    dropped_low_confidence: int = 0
    dropped_not_assessable: int = 0
    dropped_quote_missing: int = 0
    dropped_duplicate: int = 0
    errors: list[str] = field(default_factory=list)

    def add(self, other: ToneGateStats) -> None:
        self.accepted += other.accepted
        self.dropped_low_confidence += other.dropped_low_confidence
        self.dropped_not_assessable += other.dropped_not_assessable
        self.dropped_quote_missing += other.dropped_quote_missing
        self.dropped_duplicate += other.dropped_duplicate
        self.errors.extend(other.errors)


def accept_flags(
    flags: Iterable[ToneFlag],
    texts: dict[str, str],
    *,
    min_confidence: float,
    stats: ToneGateStats,
) -> list[ToneFlag]:
    """Keep the flags that pass every rule the prompt states; count the rest.

    ``texts`` maps the ids the model was asked to assess to their text. A flag
    on any other id — lead-in context, a partner message, an invented id — is
    dropped: the model only ever saw those as context. One flag per
    (message, metric); a repeat is dropped, not double-counted.
    """
    seen: set[tuple[str, str]] = set()
    kept: list[ToneFlag] = []
    for flag in flags:
        if flag.confidence < min_confidence:
            stats.dropped_low_confidence += 1
            continue
        text = texts.get(flag.message_id)
        if text is None:
            stats.dropped_not_assessable += 1
            continue
        if not quote_found(flag.quote, text):
            stats.dropped_quote_missing += 1
            continue
        key = (flag.message_id, flag.metric.value)
        if key in seen:
            stats.dropped_duplicate += 1
            continue
        seen.add(key)
        kept.append(flag)
        stats.accepted += 1
    return kept


# ---------------------------------------------------------------------------
# Payload shaping (server -> React island)
# ---------------------------------------------------------------------------


def tone_days_payload(rows: Iterable[dict[str, Any]]) -> list[dict[str, Any]]:
    """Fold ``(manager, day, metric, flagged, assessed)`` rows into one entry per
    manager-day: ``{"m": id, "d": day, "a": assessed, "f": {metric: flagged}}``.

    ``assessed`` is written identically on every metric row of a manager-day by
    the persister; ``max`` guards against a partially written day rather than
    trusting that.
    """
    grouped: dict[tuple[str, str], dict[str, Any]] = {}
    for row in rows:
        key = (str(row["manager_id"]), row["day"].isoformat())
        entry = grouped.get(key)
        if entry is None:
            entry = {"m": key[0], "d": key[1], "a": 0, "f": {}}
            grouped[key] = entry
        entry["a"] = max(entry["a"], int(row["assessed"]))
        entry["f"][str(row["metric"])] = entry["f"].get(str(row["metric"]), 0) + int(
            row["flagged"]
        )
    return [grouped[k] for k in sorted(grouped)]


def tone_flags_payload(
    rows: Iterable[dict[str, Any]], tz: tzinfo
) -> dict[str, list[dict[str, Any]]]:
    """Group flag rows by manager id for the dossier's folded list."""
    out: dict[str, list[dict[str, Any]]] = {}
    for row in rows:
        occurred: datetime = row["occurred_at"]
        out.setdefault(str(row["manager_id"]), []).append(
            {
                "id": str(row["id"]),
                "metric": str(row["metric"]),
                "day": row["day"].isoformat(),
                "at": occurred.astimezone(tz).isoformat(timespec="minutes"),
                "chatName": row.get("chat_name") or "—",
                "unitType": row.get("unit_type") or "group",
                "quote": row["quote"],
                "reason": row["reason"],
                "confidence": float(row["confidence"]),
            }
        )
    return out
