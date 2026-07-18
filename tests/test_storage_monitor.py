"""Storage-monitor worker + system storage alert tests.

No real DB, Slack, or Telegram network calls. DB and alert helpers are
monkeypatched; FakeBot stands in for aiogram.Bot.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from src.pipeline.workers import run_storage_check

_MB = 1024 * 1024


class FakeBot:
    pass


# ---------------------------------------------------------------------------
# run_storage_check: threshold crossing
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_below_threshold_no_alert_and_rearms() -> None:
    """Usage under the threshold → no alert, cursor reset to None (re-arm)."""
    bot = FakeBot()
    alert_mock = AsyncMock()
    with (
        patch("src.pipeline.workers.acquire_connection") as mock_ctx,
        patch(
            "src.pipeline.workers.get_database_size_bytes",
            new_callable=AsyncMock,
            return_value=100 * _MB,  # 100 / 500 = 20%
        ),
        patch("src.pipeline.workers.send_storage_warning_alert", alert_mock),
        patch("src.pipeline.workers.settings") as st,
    ):
        st.SUPABASE_DB_SIZE_LIMIT_MB = 500
        st.STORAGE_ALERT_THRESHOLD_PERCENT = 80
        st.STORAGE_ALERT_REPING_HOURS = 24
        mock_ctx.return_value.__aenter__ = AsyncMock(return_value=AsyncMock())
        mock_ctx.return_value.__aexit__ = AsyncMock(return_value=False)

        # Even if we'd previously alerted, dropping below re-arms.
        result = await run_storage_check(bot, datetime.now(UTC))  # type: ignore[arg-type]

    assert result is None
    alert_mock.assert_not_awaited()


@pytest.mark.asyncio
async def test_crossing_threshold_fires_alert() -> None:
    """First crossing (no prior alert) → alert fires, cursor set to now."""
    bot = FakeBot()
    alert_mock = AsyncMock()
    with (
        patch("src.pipeline.workers.acquire_connection") as mock_ctx,
        patch(
            "src.pipeline.workers.get_database_size_bytes",
            new_callable=AsyncMock,
            return_value=420 * _MB,  # 420 / 500 = 84%
        ),
        patch("src.pipeline.workers.send_storage_warning_alert", alert_mock),
        patch("src.pipeline.workers.settings") as st,
    ):
        st.SUPABASE_DB_SIZE_LIMIT_MB = 500
        st.STORAGE_ALERT_THRESHOLD_PERCENT = 80
        st.STORAGE_ALERT_REPING_HOURS = 24
        mock_ctx.return_value.__aenter__ = AsyncMock(return_value=AsyncMock())
        mock_ctx.return_value.__aexit__ = AsyncMock(return_value=False)

        before = datetime.now(UTC)
        result = await run_storage_check(bot, None)  # type: ignore[arg-type]

    alert_mock.assert_awaited_once()
    kwargs = alert_mock.call_args[1]
    assert kwargs["used_bytes"] == 420 * _MB
    assert kwargs["limit_bytes"] == 500 * _MB
    assert 83.5 < kwargs["pct"] < 84.5
    assert result is not None and result >= before


@pytest.mark.asyncio
async def test_at_exact_threshold_fires() -> None:
    """Usage == threshold is also a crossing (>= comparison)."""
    bot = FakeBot()
    alert_mock = AsyncMock()
    with (
        patch("src.pipeline.workers.acquire_connection") as mock_ctx,
        patch(
            "src.pipeline.workers.get_database_size_bytes",
            new_callable=AsyncMock,
            return_value=400 * _MB,  # 400 / 500 = exactly 80%
        ),
        patch("src.pipeline.workers.send_storage_warning_alert", alert_mock),
        patch("src.pipeline.workers.settings") as st,
    ):
        st.SUPABASE_DB_SIZE_LIMIT_MB = 500
        st.STORAGE_ALERT_THRESHOLD_PERCENT = 80
        st.STORAGE_ALERT_REPING_HOURS = 24
        mock_ctx.return_value.__aenter__ = AsyncMock(return_value=AsyncMock())
        mock_ctx.return_value.__aexit__ = AsyncMock(return_value=False)

        result = await run_storage_check(bot, None)  # type: ignore[arg-type]

    alert_mock.assert_awaited_once()
    assert result is not None


# ---------------------------------------------------------------------------
# run_storage_check: re-ping dedup
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_above_threshold_recent_alert_stays_quiet() -> None:
    """Still full but alerted within the re-ping window → no repeat, cursor kept."""
    bot = FakeBot()
    alert_mock = AsyncMock()
    recent = datetime.now(UTC) - timedelta(hours=1)
    with (
        patch("src.pipeline.workers.acquire_connection") as mock_ctx,
        patch(
            "src.pipeline.workers.get_database_size_bytes",
            new_callable=AsyncMock,
            return_value=450 * _MB,  # 90%
        ),
        patch("src.pipeline.workers.send_storage_warning_alert", alert_mock),
        patch("src.pipeline.workers.settings") as st,
    ):
        st.SUPABASE_DB_SIZE_LIMIT_MB = 500
        st.STORAGE_ALERT_THRESHOLD_PERCENT = 80
        st.STORAGE_ALERT_REPING_HOURS = 24
        mock_ctx.return_value.__aenter__ = AsyncMock(return_value=AsyncMock())
        mock_ctx.return_value.__aexit__ = AsyncMock(return_value=False)

        result = await run_storage_check(bot, recent)  # type: ignore[arg-type]

    alert_mock.assert_not_awaited()
    assert result == recent


@pytest.mark.asyncio
async def test_above_threshold_stale_alert_repings() -> None:
    """Still full and the re-ping window elapsed → warn again, cursor advances."""
    bot = FakeBot()
    alert_mock = AsyncMock()
    stale = datetime.now(UTC) - timedelta(hours=25)
    with (
        patch("src.pipeline.workers.acquire_connection") as mock_ctx,
        patch(
            "src.pipeline.workers.get_database_size_bytes",
            new_callable=AsyncMock,
            return_value=450 * _MB,  # 90%
        ),
        patch("src.pipeline.workers.send_storage_warning_alert", alert_mock),
        patch("src.pipeline.workers.settings") as st,
    ):
        st.SUPABASE_DB_SIZE_LIMIT_MB = 500
        st.STORAGE_ALERT_THRESHOLD_PERCENT = 80
        st.STORAGE_ALERT_REPING_HOURS = 24
        mock_ctx.return_value.__aenter__ = AsyncMock(return_value=AsyncMock())
        mock_ctx.return_value.__aexit__ = AsyncMock(return_value=False)

        result = await run_storage_check(bot, stale)  # type: ignore[arg-type]

    alert_mock.assert_awaited_once()
    assert result is not None and result > stale


@pytest.mark.asyncio
async def test_zero_limit_never_alerts() -> None:
    """A misconfigured (0) cap must not divide-by-zero or alert."""
    bot = FakeBot()
    alert_mock = AsyncMock()
    with (
        patch("src.pipeline.workers.acquire_connection") as mock_ctx,
        patch(
            "src.pipeline.workers.get_database_size_bytes",
            new_callable=AsyncMock,
            return_value=999 * _MB,
        ),
        patch("src.pipeline.workers.send_storage_warning_alert", alert_mock),
        patch("src.pipeline.workers.settings") as st,
    ):
        st.SUPABASE_DB_SIZE_LIMIT_MB = 0
        st.STORAGE_ALERT_THRESHOLD_PERCENT = 80
        st.STORAGE_ALERT_REPING_HOURS = 24
        mock_ctx.return_value.__aenter__ = AsyncMock(return_value=AsyncMock())
        mock_ctx.return_value.__aexit__ = AsyncMock(return_value=False)

        result = await run_storage_check(bot, None)  # type: ignore[arg-type]

    assert result is None
    alert_mock.assert_not_awaited()


# ---------------------------------------------------------------------------
# system.py: send_storage_warning_alert (Slack + TG, both best-effort)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_send_storage_alert_posts_to_alerts_channel() -> None:
    from src.alerts.system import send_storage_warning_alert

    bot = FakeBot()
    slack_mock = AsyncMock()
    slack_client = MagicMock()
    slack_client.chat_postMessage = slack_mock

    with (
        patch("src.alerts.system.get_slack_client", return_value=slack_client),
        patch("src.alerts.system.acquire_connection") as db_ctx,
        patch(
            "src.alerts.system.list_admin_users",
            new_callable=AsyncMock,
            return_value=[],
        ),
        patch("src.alerts.system.notify_admins", new_callable=AsyncMock) as dm_mock,
        patch("src.alerts.system.settings") as st,
    ):
        st.SLACK_CHANNEL_ALERTS = "C_ALERTS"
        st.STORAGE_ALERT_THRESHOLD_PERCENT = 80
        db_ctx.return_value.__aenter__ = AsyncMock(return_value=AsyncMock())
        db_ctx.return_value.__aexit__ = AsyncMock(return_value=False)

        await send_storage_warning_alert(
            bot,  # type: ignore[arg-type]
            used_bytes=450 * _MB,
            limit_bytes=500 * _MB,
            pct=90.0,
        )

    slack_mock.assert_awaited_once()
    assert slack_mock.call_args[1]["channel"] == "C_ALERTS"
    dm_mock.assert_awaited_once()


@pytest.mark.asyncio
async def test_send_storage_alert_slack_failure_does_not_raise() -> None:
    from src.alerts.system import send_storage_warning_alert

    bot = FakeBot()
    slack_client = MagicMock()
    slack_client.chat_postMessage = AsyncMock(side_effect=Exception("network error"))

    with (
        patch("src.alerts.system.get_slack_client", return_value=slack_client),
        patch("src.alerts.system.acquire_connection") as db_ctx,
        patch(
            "src.alerts.system.list_admin_users",
            new_callable=AsyncMock,
            return_value=[],
        ),
        patch("src.alerts.system.notify_admins", new_callable=AsyncMock),
        patch("src.alerts.system.settings") as st,
    ):
        st.SLACK_CHANNEL_ALERTS = "C_ALERTS"
        st.STORAGE_ALERT_THRESHOLD_PERCENT = 80
        db_ctx.return_value.__aenter__ = AsyncMock(return_value=AsyncMock())
        db_ctx.return_value.__aexit__ = AsyncMock(return_value=False)

        # Must not raise (Slack down still lets the TG DMs through).
        await send_storage_warning_alert(
            bot,  # type: ignore[arg-type]
            used_bytes=450 * _MB,
            limit_bytes=500 * _MB,
            pct=90.0,
        )


@pytest.mark.asyncio
async def test_send_storage_alert_tg_failure_does_not_raise() -> None:
    from src.alerts.system import send_storage_warning_alert

    bot = FakeBot()
    slack_client = MagicMock()
    slack_client.chat_postMessage = AsyncMock()

    with (
        patch("src.alerts.system.get_slack_client", return_value=slack_client),
        patch("src.alerts.system.acquire_connection") as db_ctx,
        patch(
            "src.alerts.system.list_admin_users",
            new_callable=AsyncMock,
            side_effect=RuntimeError("db"),
        ),
        patch("src.alerts.system.notify_admins", new_callable=AsyncMock),
        patch("src.alerts.system.settings") as st,
    ):
        st.SLACK_CHANNEL_ALERTS = "C_ALERTS"
        st.STORAGE_ALERT_THRESHOLD_PERCENT = 80
        db_ctx.return_value.__aenter__ = AsyncMock(return_value=AsyncMock())
        db_ctx.return_value.__aexit__ = AsyncMock(return_value=False)

        # Must not raise even when DB (and therefore TG DMs) fail.
        await send_storage_warning_alert(
            bot,  # type: ignore[arg-type]
            used_bytes=450 * _MB,
            limit_bytes=500 * _MB,
            pct=90.0,
        )
