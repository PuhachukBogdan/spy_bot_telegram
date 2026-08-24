"""Whose risk case is it. Pure and synchronous — no I/O.

A risk case appears on a manager's dossier because the CHAT is his, even when the
risky message came from the partner. Those cases are context, not conduct, and
must never move his numbers (PHASE2_MANAGER_KPI.md §5.4).

The distinction is encoded once, here, as :meth:`RiskAttribution.counts`. If it
were left to each call site to remember, one forgotten check would quietly start
scoring managers for what partners wrote.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from enum import StrEnum
from uuid import UUID

from src.db.models import InternalUser


class RiskAttribution(StrEnum):
    """How a risk case relates to the manager whose page it appears on."""

    #: The risky message was written by this manager. Counts.
    MANAGER_ACTION = "manager_action"
    #: Raised by a partner (or anyone not this manager) in a chat he owns.
    #: Shown for context, never counted.
    CHAT_CONTEXT = "chat_context"

    @property
    def counts(self) -> bool:
        """Whether this case may influence any metric. Only conduct counts."""
        return self is RiskAttribution.MANAGER_ACTION


def build_manager_index(managers: Iterable[InternalUser]) -> dict[int, UUID]:
    """Telegram user id -> manager id, for O(1) attribution over many events.

    One person may hold several Telegram accounts (``telegram_accounts`` is an
    array), so a manager can contribute more than one entry.
    """
    index: dict[int, UUID] = {}
    for manager in managers:
        for telegram_id in manager.telegram_accounts:
            index[telegram_id] = manager.id
    return index


def attribute_risk(
    sender_id: int | None,
    manager_index: Mapping[int, UUID],
) -> tuple[RiskAttribution, UUID | None]:
    """Classify one risk event by its author.

    ``sender_id`` is ``risk_events.sender_id`` — the Telegram id of the author of
    the anchor message, recorded at detection time, so no join back through
    ``messages`` is needed.

    A missing ``sender_id`` (anonymous admin, channel post, older row) is context,
    not conduct: absent proof that a manager wrote it, we do not charge him for it.
    """
    if sender_id is not None:
        manager_id = manager_index.get(sender_id)
        if manager_id is not None:
            return RiskAttribution.MANAGER_ACTION, manager_id
    return RiskAttribution.CHAT_CONTEXT, None
