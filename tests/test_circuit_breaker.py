"""Phase 14-15: cost circuit breaker + system alert tests.

No real DB, Slack, or Telegram network calls. DB and alert helpers are
monkeypatched; FakeBot stands in for aiogram.Bot.
"""

from __future__ import annotations

from datetime import date
from decimal import Decimal
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from src.db.models import CostTracking
from src.pipeline.workers import _budget_gate

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


class FakeBot:
    pass


def _make_cost(total: str, breaker: bool = False) -> CostTracking:
    return CostTracking(
        date=date(2026, 6, 9),
        llm_cost_usd=Decimal(total),
        llm_calls_count=1,
        whisper_cost_usd=Decimal("0"),
        whisper_calls_count=0,
        total_cost_usd=Decimal(total),
        circuit_breaker_triggered=breaker,
    )


# ---------------------------------------------------------------------------
# is_circuit_open short-circuit
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_budget_gate_returns_true_when_circuit_already_open() -> None:
    bot = FakeBot()
    with (
        patch("src.pipeline.workers.acquire_connection") as mock_ctx,
        patch("src.pipeline.workers.is_circuit_open", new_callable=AsyncMock, return_value=True),
    ):
        mock_conn = AsyncMock()
        mock_ctx.return_value.__aenter__ = AsyncMock(return_value=mock_conn)
        mock_ctx.return_value.__aexit__ = AsyncMock(return_value=False)

        result = await _budget_gate(bot, "analysis")  # type: ignore[arg-type]

    assert result is True


@pytest.mark.asyncio
async def test_budget_gate_returns_false_when_circuit_closed_and_under_budget() -> None:
    bot = FakeBot()
    today = _make_cost("5.00")

    with (
        patch("src.pipeline.workers.acquire_connection") as mock_ctx,
        patch("src.pipeline.workers.is_circuit_open", new_callable=AsyncMock, return_value=False),
        patch("src.pipeline.workers.get_today", new_callable=AsyncMock, return_value=today),
    ):
        mock_conn = AsyncMock()
        mock_ctx.return_value.__aenter__ = AsyncMock(return_value=mock_conn)
        mock_ctx.return_value.__aexit__ = AsyncMock(return_value=False)

        result = await _budget_gate(bot, "analysis")  # type: ignore[arg-type]

    assert result is False


# ---------------------------------------------------------------------------
# Budget exceeded → newly tripped
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_budget_gate_trips_and_alerts_when_budget_exceeded() -> None:
    bot = FakeBot()
    today = _make_cost("30.50")

    alert_mock = AsyncMock()
    with (
        patch("src.pipeline.workers.acquire_connection") as mock_ctx,
        patch("src.pipeline.workers.is_circuit_open", new_callable=AsyncMock, return_value=False),
        patch("src.pipeline.workers.get_today", new_callable=AsyncMock, return_value=today),
        patch("src.pipeline.workers.trip_circuit_breaker", new_callable=AsyncMock, return_value=True),
        patch("src.pipeline.workers.send_budget_exceeded_alert", alert_mock),
    ):
        mock_conn = AsyncMock()
        mock_ctx.return_value.__aenter__ = AsyncMock(return_value=mock_conn)
        mock_ctx.return_value.__aexit__ = AsyncMock(return_value=False)

        result = await _budget_gate(bot, "analysis")  # type: ignore[arg-type]

    assert result is True
    alert_mock.assert_awaited_once()
    _spend, _limit = alert_mock.call_args[0][1], alert_mock.call_args[0][2]
    assert _spend == Decimal("30.50")
    assert _limit == Decimal("30")


@pytest.mark.asyncio
async def test_budget_gate_exact_budget_triggers() -> None:
    """Spend == limit is also over budget."""
    bot = FakeBot()
    today = _make_cost("30.00")

    alert_mock = AsyncMock()
    with (
        patch("src.pipeline.workers.acquire_connection") as mock_ctx,
        patch("src.pipeline.workers.is_circuit_open", new_callable=AsyncMock, return_value=False),
        patch("src.pipeline.workers.get_today", new_callable=AsyncMock, return_value=today),
        patch("src.pipeline.workers.trip_circuit_breaker", new_callable=AsyncMock, return_value=True),
        patch("src.pipeline.workers.send_budget_exceeded_alert", alert_mock),
    ):
        mock_conn = AsyncMock()
        mock_ctx.return_value.__aenter__ = AsyncMock(return_value=mock_conn)
        mock_ctx.return_value.__aexit__ = AsyncMock(return_value=False)

        result = await _budget_gate(bot, "analysis")  # type: ignore[arg-type]

    assert result is True


@pytest.mark.asyncio
async def test_budget_gate_already_tripped_no_duplicate_alert() -> None:
    """trip_circuit_breaker returns False → alert not fired a second time."""
    bot = FakeBot()
    today = _make_cost("35.00")

    alert_mock = AsyncMock()
    with (
        patch("src.pipeline.workers.acquire_connection") as mock_ctx,
        patch("src.pipeline.workers.is_circuit_open", new_callable=AsyncMock, return_value=False),
        patch("src.pipeline.workers.get_today", new_callable=AsyncMock, return_value=today),
        patch("src.pipeline.workers.trip_circuit_breaker", new_callable=AsyncMock, return_value=False),
        patch("src.pipeline.workers.send_budget_exceeded_alert", alert_mock),
    ):
        mock_conn = AsyncMock()
        mock_ctx.return_value.__aenter__ = AsyncMock(return_value=mock_conn)
        mock_ctx.return_value.__aexit__ = AsyncMock(return_value=False)

        result = await _budget_gate(bot, "analysis")  # type: ignore[arg-type]

    assert result is True
    alert_mock.assert_not_awaited()


# ---------------------------------------------------------------------------
# No row for today (first call of the day)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_budget_gate_no_cost_row_returns_false() -> None:
    """get_today() returns None (no spend yet) → gate allows work."""
    bot = FakeBot()

    with (
        patch("src.pipeline.workers.acquire_connection") as mock_ctx,
        patch("src.pipeline.workers.is_circuit_open", new_callable=AsyncMock, return_value=False),
        patch("src.pipeline.workers.get_today", new_callable=AsyncMock, return_value=None),
    ):
        mock_conn = AsyncMock()
        mock_ctx.return_value.__aenter__ = AsyncMock(return_value=mock_conn)
        mock_ctx.return_value.__aexit__ = AsyncMock(return_value=False)

        result = await _budget_gate(bot, "file_analysis")  # type: ignore[arg-type]

    assert result is False


# ---------------------------------------------------------------------------
# Gate error handling
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_budget_gate_error_returns_false() -> None:
    """A DB error in the gate itself must NOT block worker ticks."""
    bot = FakeBot()

    with patch("src.pipeline.workers.acquire_connection") as mock_ctx:
        mock_ctx.return_value.__aenter__ = AsyncMock(side_effect=RuntimeError("db down"))
        mock_ctx.return_value.__aexit__ = AsyncMock(return_value=False)

        result = await _budget_gate(bot, "analysis")  # type: ignore[arg-type]

    assert result is False


# ---------------------------------------------------------------------------
# system.py: send_budget_exceeded_alert (Slack + TG, both best-effort)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_send_budget_alert_posts_to_slack_channel_system() -> None:
    from src.alerts.system import send_budget_exceeded_alert

    bot = FakeBot()
    slack_mock = AsyncMock()
    slack_client = MagicMock()
    slack_client.chat_postMessage = slack_mock

    with (
        patch("src.alerts.system.get_slack_client", return_value=slack_client),
        patch("src.alerts.system.acquire_connection") as db_ctx,
        patch("src.alerts.system.list_admin_users", new_callable=AsyncMock, return_value=[]),
        patch("src.alerts.system.notify_admins", new_callable=AsyncMock),
        patch("src.alerts.system.settings") as mock_settings,
    ):
        mock_settings.SLACK_CHANNEL_SYSTEM = "C_SYSTEM"
        mock_settings.SLACK_CHANNEL_ALERTS = "C_ALERTS"
        mock_conn = AsyncMock()
        db_ctx.return_value.__aenter__ = AsyncMock(return_value=mock_conn)
        db_ctx.return_value.__aexit__ = AsyncMock(return_value=False)

        await send_budget_exceeded_alert(bot, Decimal("31.23"), Decimal("30"))  # type: ignore[arg-type]

    slack_mock.assert_awaited_once()
    call_kwargs = slack_mock.call_args[1]
    assert call_kwargs["channel"] == "C_SYSTEM"


@pytest.mark.asyncio
async def test_send_budget_alert_falls_back_to_alerts_channel() -> None:
    from src.alerts.system import send_budget_exceeded_alert

    bot = FakeBot()
    slack_mock = AsyncMock()
    slack_client = MagicMock()
    slack_client.chat_postMessage = slack_mock

    with (
        patch("src.alerts.system.get_slack_client", return_value=slack_client),
        patch("src.alerts.system.acquire_connection") as db_ctx,
        patch("src.alerts.system.list_admin_users", new_callable=AsyncMock, return_value=[]),
        patch("src.alerts.system.notify_admins", new_callable=AsyncMock),
        patch("src.alerts.system.settings") as mock_settings,
    ):
        mock_settings.SLACK_CHANNEL_SYSTEM = None
        mock_settings.SLACK_CHANNEL_ALERTS = "C_ALERTS"
        mock_conn = AsyncMock()
        db_ctx.return_value.__aenter__ = AsyncMock(return_value=mock_conn)
        db_ctx.return_value.__aexit__ = AsyncMock(return_value=False)

        await send_budget_exceeded_alert(bot, Decimal("30.01"), Decimal("30"))  # type: ignore[arg-type]

    slack_mock.assert_awaited_once()
    assert slack_mock.call_args[1]["channel"] == "C_ALERTS"


@pytest.mark.asyncio
async def test_send_budget_alert_slack_failure_does_not_raise() -> None:
    from src.alerts.system import send_budget_exceeded_alert

    bot = FakeBot()
    slack_client = MagicMock()
    slack_client.chat_postMessage = AsyncMock(side_effect=Exception("network error"))

    with (
        patch("src.alerts.system.get_slack_client", return_value=slack_client),
        patch("src.alerts.system.acquire_connection") as db_ctx,
        patch("src.alerts.system.list_admin_users", new_callable=AsyncMock, return_value=[]),
        patch("src.alerts.system.notify_admins", new_callable=AsyncMock),
        patch("src.alerts.system.settings") as mock_settings,
    ):
        mock_settings.SLACK_CHANNEL_SYSTEM = "C_SYSTEM"
        mock_settings.SLACK_CHANNEL_ALERTS = "C_ALERTS"
        mock_conn = AsyncMock()
        db_ctx.return_value.__aenter__ = AsyncMock(return_value=mock_conn)
        db_ctx.return_value.__aexit__ = AsyncMock(return_value=False)

        # Must not raise
        await send_budget_exceeded_alert(bot, Decimal("35.00"), Decimal("30"))  # type: ignore[arg-type]


@pytest.mark.asyncio
async def test_send_budget_alert_tg_failure_does_not_raise() -> None:
    from src.alerts.system import send_budget_exceeded_alert

    bot = FakeBot()
    slack_client = MagicMock()
    slack_client.chat_postMessage = AsyncMock()

    with (
        patch("src.alerts.system.get_slack_client", return_value=slack_client),
        patch("src.alerts.system.acquire_connection") as db_ctx,
        patch("src.alerts.system.list_admin_users", new_callable=AsyncMock, side_effect=RuntimeError("db")),
        patch("src.alerts.system.notify_admins", new_callable=AsyncMock),
        patch("src.alerts.system.settings") as mock_settings,
    ):
        mock_settings.SLACK_CHANNEL_SYSTEM = "C_SYSTEM"
        mock_settings.SLACK_CHANNEL_ALERTS = "C_ALERTS"
        mock_conn = AsyncMock()
        db_ctx.return_value.__aenter__ = AsyncMock(return_value=mock_conn)
        db_ctx.return_value.__aexit__ = AsyncMock(return_value=False)

        # Must not raise even when DB (and therefore TG DMs) fail
        await send_budget_exceeded_alert(bot, Decimal("30.01"), Decimal("30"))  # type: ignore[arg-type]
