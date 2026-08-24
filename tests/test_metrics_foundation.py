"""Phase 2 foundation: epoch-clamped windows, risk attribution, real managers.

No real DB. The connection fake returns canned rows, matching tests/test_daily.py.
"""

from __future__ import annotations

from dataclasses import FrozenInstanceError
from datetime import UTC, date, datetime, timedelta
from typing import Any
from uuid import UUID, uuid4

import pytest

from src.db.models import InternalUser
from src.db.queries.etc import list_real_managers
from src.metrics.attribution import (
    RiskAttribution,
    attribute_risk,
    build_manager_index,
)
from src.metrics.window import MetricsWindow, epoch_floor, resolve_metrics_window

EPOCH = date(2026, 8, 20)


def _dt(day: int, hour: int = 0) -> datetime:
    return datetime(2026, 8, day, hour, tzinfo=UTC)


# ---------------------------------------------------------------------------
# window: the epoch floor
# ---------------------------------------------------------------------------


def test_window_untouched_when_fully_after_epoch() -> None:
    w = resolve_metrics_window(_dt(25), _dt(27), epoch=EPOCH)
    assert (w.since, w.until) == (_dt(25), _dt(27))
    assert w.is_empty is False


def test_window_start_clamped_to_epoch() -> None:
    # Asked for 15th–25th; the epoch is the 20th, so measurement starts there.
    w = resolve_metrics_window(_dt(15), _dt(25), epoch=EPOCH)
    assert w.since == epoch_floor(EPOCH)
    assert w.until == _dt(25)


def test_no_epoch_means_no_floor() -> None:
    w = resolve_metrics_window(_dt(1), _dt(5), epoch=None)
    assert (w.since, w.until) == (_dt(1), _dt(5))
    assert w.has_comparison is True  # nothing to reach back past


def test_window_entirely_before_epoch_is_empty() -> None:
    w = resolve_metrics_window(_dt(10), _dt(12), epoch=EPOCH)
    assert w.is_empty is True
    assert w.has_comparison is False
    assert w.length == timedelta(0)


def test_naive_datetimes_are_treated_as_utc() -> None:
    # A naive/aware comparison would raise TypeError inside a scheduled job,
    # surfacing as a report that silently never generated.
    naive = datetime(2026, 8, 25)
    w = resolve_metrics_window(naive, datetime(2026, 8, 27), epoch=EPOCH)
    assert w.since == _dt(25)
    assert w.until == _dt(27)


def test_epoch_floor_is_midnight_utc() -> None:
    assert epoch_floor(EPOCH) == datetime(2026, 8, 20, tzinfo=UTC)
    assert epoch_floor(None) is None


# ---------------------------------------------------------------------------
# window: the comparison base
# ---------------------------------------------------------------------------


def test_comparison_is_equal_length_and_tiles_exactly() -> None:
    w = resolve_metrics_window(_dt(24), _dt(26), epoch=EPOCH)
    assert w.previous == (_dt(22), _dt(24))
    # Contiguous: the base ends exactly where the window starts — no gap, no overlap.
    assert w.previous is not None and w.previous[1] == w.since
    assert w.previous[1] - w.previous[0] == w.length


def test_comparison_suppressed_when_base_predates_epoch() -> None:
    # 21st–23rd would compare against 19th–21st, but the epoch is the 20th.
    w = resolve_metrics_window(_dt(21), _dt(23), epoch=EPOCH)
    assert w.has_comparison is False
    assert w.previous is None


def test_comparison_allowed_when_base_starts_exactly_on_epoch() -> None:
    w = resolve_metrics_window(_dt(22), _dt(24), epoch=EPOCH)
    assert w.previous == (_dt(20), _dt(22))


def test_comparison_length_follows_the_clamped_window() -> None:
    # Requested 18th–24th (6d) but clamped to 20th–24th (4d): the base must match
    # what is actually displayed, not what was asked for, or the delta compares
    # unequal spans.
    w = resolve_metrics_window(_dt(18), _dt(24), epoch=EPOCH)
    assert w.length == timedelta(days=4)
    assert w.has_comparison is False  # 16th–20th reaches past the epoch


# ---------------------------------------------------------------------------
# attribution
# ---------------------------------------------------------------------------


def _manager(*telegram_ids: int, name: str = "Mirror | Betonwin") -> InternalUser:
    return InternalUser(
        id=uuid4(),
        full_name=name,
        role="manager",
        telegram_accounts=list(telegram_ids),
        created_at=datetime.now(UTC),
    )


def test_manager_authored_risk_counts() -> None:
    manager = _manager(8592696398)
    index = build_manager_index([manager])
    attribution, owner = attribute_risk(8592696398, index)
    assert attribution is RiskAttribution.MANAGER_ACTION
    assert owner == manager.id
    assert attribution.counts is True


def test_partner_authored_risk_is_context_and_never_counts() -> None:
    index = build_manager_index([_manager(8592696398)])
    attribution, owner = attribute_risk(555000111, index)
    assert attribution is RiskAttribution.CHAT_CONTEXT
    assert owner is None
    # The whole point of §5.4: it shows on his page but cannot move his numbers.
    assert attribution.counts is False


def test_missing_sender_is_context_not_conduct() -> None:
    index = build_manager_index([_manager(8592696398)])
    assert attribute_risk(None, index)[0] is RiskAttribution.CHAT_CONTEXT


def test_manager_with_several_telegram_accounts() -> None:
    manager = _manager(111, 222)
    index = build_manager_index([manager])
    assert attribute_risk(111, index)[1] == manager.id
    assert attribute_risk(222, index)[1] == manager.id


def test_index_is_empty_for_stub_managers() -> None:
    # A stub has no telegram account, so it contributes nothing and can never be
    # credited with an action.
    assert build_manager_index([_manager(name="78516")]) == {}


@pytest.mark.parametrize(
    ("attribution", "counts"),
    [(RiskAttribution.MANAGER_ACTION, True), (RiskAttribution.CHAT_CONTEXT, False)],
)
def test_counts_rule_is_exhaustive(attribution: RiskAttribution, counts: bool) -> None:
    assert attribution.counts is counts


# ---------------------------------------------------------------------------
# list_real_managers
# ---------------------------------------------------------------------------


class _FakeConn:
    """Captures the SQL and returns canned rows."""

    def __init__(self, rows: list[dict[str, Any]]) -> None:
        self.rows = rows
        self.sql = ""

    async def fetch(self, sql: str, *args: Any) -> list[dict[str, Any]]:
        self.sql = " ".join(sql.split())
        return self.rows


def _row(name: str, accounts: list[int]) -> dict[str, Any]:
    return {
        "id": uuid4(),
        "full_name": name,
        "role": "manager",
        "telegram_accounts": accounts,
        "enabled": True,
        "is_test": False,
        "work_timezone": "UTC",
        "created_at": datetime.now(UTC),
    }


@pytest.mark.asyncio
async def test_list_real_managers_parses_rows() -> None:
    conn = _FakeConn([_row("Mirror | Betonwin", [8592696398])])
    managers = await list_real_managers(conn)  # type: ignore[arg-type]
    assert [m.full_name for m in managers] == ["Mirror | Betonwin"]
    assert managers[0].telegram_accounts == [8592696398]
    assert isinstance(managers[0].id, UUID)


@pytest.mark.asyncio
async def test_list_real_managers_excludes_stubs_disabled_and_test() -> None:
    conn = _FakeConn([])
    await list_real_managers(conn)  # type: ignore[arg-type]
    sql = conn.sql
    # The stub discriminator — without it the axis fills with aff_id rows, which
    # is exactly what sank the manager-centric report in June.
    assert "jsonb_array_length(COALESCE(telegram_accounts, '[]'::jsonb)) > 0" in sql
    assert "role = 'manager'" in sql
    assert "enabled = true" in sql
    assert "COALESCE(is_test, false) = false" in sql


@pytest.mark.asyncio
async def test_is_test_defaults_false_when_column_absent() -> None:
    # Older rows / environments without migration 0022 must not blow up parsing.
    row = _row("Legacy", [1])
    del row["is_test"]
    managers = await list_real_managers(_FakeConn([row]))  # type: ignore[arg-type]
    assert managers[0].is_test is False


def test_metrics_window_is_frozen() -> None:
    w = MetricsWindow(since=_dt(20), until=_dt(21), previous=None)
    with pytest.raises(FrozenInstanceError):
        w.since = _dt(22)  # type: ignore[misc]
