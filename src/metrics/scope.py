"""Who is looking, and therefore what the page is allowed to contain.

The Team summary has two audiences. An **admin** sees the whole team and the risk
report. A **head** — the department lead — sees every manager EXCEPT themselves.
That single exclusion is the whole module.

It is enforced by removing data, never by hiding it in the browser: the excluded
manager is absent from the payload, so there is nothing to find in the page
source. Three separate paths carry a person into the page, and each is cut here:

1. **By ownership** — SLA, offline, coverage, chats, proposals and the risk cases
   raised in their chats all hang off ``chats.authorized_by``. Dropping the
   manager from the roster and filtering every ``manager_id``-keyed row removes
   these, and because the team series is summed from the same rows (see
   ``trends.build_scope_days``), the team totals lose them too.
2. **By authorship** — a risk case the viewer wrote in a *colleague's* chat is
   attributed to the colleague and would otherwise appear on that colleague's
   page, quoting the viewer. Those rows are matched on the author's Telegram id
   and dropped.
3. **By tone** — tone counters and flags are keyed by the author, so filtering
   the viewer's manager id covers them.

The risk report (the classic chat-centric weekly/monthly page) is admin-only: it
is rendered from stored HTML snapshots, which cannot be re-scoped per viewer, so
a head gets no mode switch and the route refuses them.
"""

from __future__ import annotations

from collections.abc import Iterable, Sequence
from dataclasses import dataclass, field
from typing import Any
from uuid import UUID

#: Roles allowed to open the dashboard at all.
DASHBOARD_ROLES: frozenset[str] = frozenset({"admin", "head"})


@dataclass(frozen=True)
class PageScope:
    """What one viewer may see. Default = an admin: everything."""

    role: str = "admin"
    viewer_id: UUID | None = None
    viewer_name: str = ""
    #: Managers removed from the roster, the sums and every keyed row.
    hidden_manager_ids: frozenset[UUID] = field(default_factory=frozenset)
    #: Telegram ids whose authored risk cases are removed.
    hidden_telegram_ids: frozenset[int] = field(default_factory=frozenset)

    @property
    def sees_risk_report(self) -> bool:
        """Only an admin gets the classic risk report mode."""
        return self.role == "admin"

    @property
    def cache_key(self) -> str:
        """Cache slot. Two viewers of the same role see different pages when they
        hide different people, so the viewer is part of the key — an admin's page
        must never be served to a head from cache."""
        if not self.hidden_manager_ids:
            return self.role
        return f"{self.role}:{self.viewer_id.hex if self.viewer_id else '-'}"

    def to_payload(self) -> dict[str, Any]:
        """The ``viewer`` block of the metrics document."""
        return {
            "name": self.viewer_name,
            "role": self.role,
            "seesRiskReport": self.sees_risk_report,
        }


#: The unrestricted view. Used by the admin route and by local rendering.
ADMIN_SCOPE = PageScope()


def scope_for(user: Any) -> PageScope:
    """Build the scope for a signed-in ``InternalUser``.

    A head hides themselves; an admin hides nobody. Any other role never reaches
    here — the route checks :data:`DASHBOARD_ROLES` first.
    """
    if user.role == "head":
        return PageScope(
            role="head",
            viewer_id=user.id,
            viewer_name=user.full_name,
            hidden_manager_ids=frozenset({user.id}),
            hidden_telegram_ids=frozenset(user.telegram_accounts),
        )
    return PageScope(role="admin", viewer_id=user.id, viewer_name=user.full_name)


def visible_managers(managers: Sequence[Any], scope: PageScope) -> list[Any]:
    """The roster this viewer gets."""
    if not scope.hidden_manager_ids:
        return list(managers)
    return [m for m in managers if m.id not in scope.hidden_manager_ids]


def visible_rows(
    rows: Iterable[dict[str, Any]], scope: PageScope, *, key: str = "manager_id"
) -> list[dict[str, Any]]:
    """Drop rows belonging to a hidden manager.

    Applied to the RAW query results rather than to assembled metrics, because
    the team series is summed from these same rows: filtering later would leave
    the hidden manager inside every team total.
    """
    if not scope.hidden_manager_ids:
        return list(rows)
    return [r for r in rows if r.get(key) not in scope.hidden_manager_ids]


def visible_risk_rows(
    rows: Iterable[dict[str, Any]], scope: PageScope
) -> list[dict[str, Any]]:
    """Risk rows minus those in a hidden manager's chat AND those they wrote.

    The second half is what keeps a head from reading their own words: a case
    they authored in a colleague's chat lives on the colleague's page, complete
    with the quote.
    """
    if not scope.hidden_manager_ids and not scope.hidden_telegram_ids:
        return list(rows)
    return [
        r
        for r in rows
        if r.get("manager_id") not in scope.hidden_manager_ids
        and r.get("sender_id") not in scope.hidden_telegram_ids
    ]


def visible_tone(tone: dict[str, Any] | None, scope: PageScope) -> dict[str, Any] | None:
    """Tone block with the hidden manager's day counters and flags removed."""
    if tone is None or not scope.hidden_manager_ids:
        return tone
    hidden = {mid.hex for mid in scope.hidden_manager_ids} | {
        str(mid) for mid in scope.hidden_manager_ids
    }
    return {
        **tone,
        "days": [d for d in tone["days"] if d["m"] not in hidden],
        "flags": {k: v for k, v in tone["flags"].items() if k not in hidden},
    }
