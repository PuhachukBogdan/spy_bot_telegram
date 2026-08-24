"""SLA response bands. Pure and synchronous — no I/O, no LLM.

A message from anyone-but-staff starts a timer; the manager's first reply stops
it. Deliberately plain: because timers only start inside working hours, the whole
interval sits within one workday, so ordinary wall-clock seconds are the measure
and no cross-day work-minute arithmetic is involved.

Two judgements are built in, and both exist to stop the metric punishing good
behaviour:

* **A substantial reply buys a little time.** Typing a real answer takes longer
  than typing "ок". Without the grace band the metric would reward the fastest
  possible non-answer and penalise the person who actually explained something —
  the exact opposite of what the tone-of-voice track rewards.
* **Silence past the offline threshold is absence, not slowness.** Twenty minutes
  of nothing means the manager was away, and averaging that into a response-time
  percentage measures presence while pretending to measure speed. It gets its own
  counter instead.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum

from src.config import settings


class SlaOutcome(StrEnum):
    """How one awaited reply turned out."""

    #: Answered inside the threshold.
    MET = "met"
    #: Slower than the threshold, but a substantial reply inside the grace cap.
    MET_SUBSTANTIVE = "met_substantive"
    #: Too slow, and not excused by the length of the reply.
    MISSED = "missed"
    #: Nothing for the offline window — treated as away, never as "slow".
    OFFLINE = "offline"

    @property
    def is_met(self) -> bool:
        return self in (SlaOutcome.MET, SlaOutcome.MET_SUBSTANTIVE)

    @property
    def in_ratio(self) -> bool:
        """Whether this outcome belongs in the SLA percentage at all.

        ``OFFLINE`` does not: mixing absence into a speed metric would let one
        long lunch outweigh a morning of fast replies, and would hide the very
        thing worth seeing — that nobody was there.
        """
        return self is not SlaOutcome.OFFLINE


@dataclass(frozen=True)
class SlaThresholds:
    """The four numbers that define the bands. Injectable so tests need no env."""

    threshold_seconds: int
    substantive_grace_seconds: int
    substantive_reply_chars: int
    offline_after_seconds: int

    @classmethod
    def from_settings(cls) -> SlaThresholds:
        return cls(
            threshold_seconds=settings.SLA_RESPONSE_THRESHOLD_SECONDS,
            substantive_grace_seconds=settings.SLA_SUBSTANTIVE_GRACE_SECONDS,
            substantive_reply_chars=settings.SLA_SUBSTANTIVE_REPLY_CHARS,
            offline_after_seconds=settings.SLA_OFFLINE_AFTER_SECONDS,
        )


def classify_response(
    waited_seconds: float | None,
    reply_chars: int | None,
    *,
    thresholds: SlaThresholds | None = None,
) -> SlaOutcome:
    """Place one awaited reply into a band.

    ``waited_seconds`` is ``None`` when no reply ever came; ``reply_chars`` is the
    length of the reply that stopped the timer (``None`` when there was none).

    Order matters: the offline check runs first, so a reply that finally arrives
    after half an hour is recorded as absence rather than as an extreme miss —
    otherwise a single forgotten chat would dominate the average and the real
    signal (nobody was at the desk) would be lost inside a percentage.
    """
    limits = SlaThresholds.from_settings() if thresholds is None else thresholds

    if waited_seconds is None or waited_seconds >= limits.offline_after_seconds:
        return SlaOutcome.OFFLINE
    if waited_seconds <= limits.threshold_seconds:
        return SlaOutcome.MET
    if (
        reply_chars is not None
        and reply_chars > limits.substantive_reply_chars
        and waited_seconds <= limits.substantive_grace_seconds
    ):
        return SlaOutcome.MET_SUBSTANTIVE
    return SlaOutcome.MISSED


@dataclass(frozen=True)
class SlaTally:
    """Aggregated outcomes for one manager over one window."""

    met: int = 0
    met_substantive: int = 0
    missed: int = 0
    offline: int = 0

    @property
    def rated(self) -> int:
        """Replies that count toward the percentage (everything but absences)."""
        return self.met + self.met_substantive + self.missed

    @property
    def percent(self) -> float | None:
        """SLA %, or ``None`` when nothing was rated.

        ``None`` rather than ``0.0`` on purpose: a manager with no partner
        messages this period has not failed, and a zero would read as if he had.
        """
        if self.rated == 0:
            return None
        return round(100 * (self.met + self.met_substantive) / self.rated, 1)


def tally(outcomes: list[SlaOutcome]) -> SlaTally:
    """Count outcomes into a :class:`SlaTally`."""
    counts = {outcome: 0 for outcome in SlaOutcome}
    for outcome in outcomes:
        counts[outcome] += 1
    return SlaTally(
        met=counts[SlaOutcome.MET],
        met_substantive=counts[SlaOutcome.MET_SUBSTANTIVE],
        missed=counts[SlaOutcome.MISSED],
        offline=counts[SlaOutcome.OFFLINE],
    )
