"""The membership sweep: ask Telegram who of our people is in which chat.

Why a sweep at all. Telegram tells a bot about joins and leaves only when it
happens to: ``new_chat_members`` service messages are sent for additions, not
for every invite-link join; ``chat_member`` updates reach the bot only in chats
where it is an administrator; and nothing at all describes who was already in
a chat before the bot arrived. The database therefore knew the ADDER of each
chat and little else — while the real picture (measured 2026-10-06) was 3–5
managers per group and the head of department in 270 of 317 groups.

What one pass does, per active group (forum topics share their group's answer):

1. ``getChat`` — the current title. If it differs from ``chats.chat_name`` the
   row is updated (``update_chat_title``). Groups get renamed in bulk (the
   2026-09 rebrand touched 161 of 317) and the old code never followed.
2. ``getChatMember`` for every staff account (``list_staff_accounts`` — every
   Telegram id of every real admin / head / manager, disabled and deactivated
   people included). Telegram's status is written as-is
   (``record_verified_status``); ``left`` / ``kicked`` rows are kept, not
   deleted, so ``first_seen_at`` survives.

Both are READ calls. This module never sends anything to a partner chat and
has no business doing so (CLAUDE.md §1).

Pacing: ``MEMBERSHIP_SWEEP_CONCURRENCY`` accounts are asked at once per chat
and chats go one after another; a ``RetryAfter`` from Telegram is honoured.
A full pass over ~320 groups x ~13 accounts is a few minutes.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any

from aiogram import Bot
from aiogram.exceptions import (
    TelegramAPIError,
    TelegramBadRequest,
    TelegramForbiddenError,
    TelegramRetryAfter,
)

from src.config import settings
from src.db.client import acquire_connection
from src.db.queries.chat_members import (
    StaffAccount,
    SweepTarget,
    is_present,
    list_staff_accounts,
    list_sweep_targets,
    record_verified_status,
)
from src.db.queries.chats import update_chat_title
from src.utils.logging import get_logger

log = get_logger(__name__)

#: How many times one Telegram call is retried on a transient API error.
_ATTEMPTS = 4


@dataclass
class SweepStats:
    """What one pass did — for the log line and the one-off script."""

    chats: int = 0
    chats_unreachable: int = 0
    titles_updated: int = 0
    calls: int = 0
    present: int = 0
    absent: int = 0
    errors: int = 0
    unreachable: list[int] = field(default_factory=list)


async def _call(coro_factory: Any, stats: SweepStats) -> tuple[str, Any]:
    """Run one Telegram call with RetryAfter / transient-error handling.

    Returns ``("ok", result)``, ``("forbidden", exc)`` (bot not in the chat),
    ``("bad", exc)`` (Telegram refused the question) or ``("error", exc)``.
    """
    delay = 1.0
    last: BaseException | None = None
    for attempt in range(_ATTEMPTS):
        try:
            stats.calls += 1
            return "ok", await coro_factory()
        except TelegramRetryAfter as exc:
            await asyncio.sleep(exc.retry_after + 0.5)
            last = exc
        except TelegramForbiddenError as exc:
            return "forbidden", exc
        except TelegramBadRequest as exc:
            return "bad", exc
        except TelegramAPIError as exc:
            last = exc
            if attempt == _ATTEMPTS - 1:
                break
            await asyncio.sleep(delay)
            delay *= 2
    return "error", last


async def _sweep_chat(
    bot: Bot,
    target: SweepTarget,
    accounts: list[StaffAccount],
    stats: SweepStats,
    *,
    semaphore: asyncio.Semaphore,
    now: datetime,
) -> None:
    kind, chat = await _call(lambda: bot.get_chat(target.telegram_chat_id), stats)
    if kind != "ok":
        stats.chats_unreachable += 1
        stats.unreachable.append(target.telegram_chat_id)
        log.info(
            "membership.chat_unreachable",
            chat_id=target.telegram_chat_id,
            reason=kind,
            error=str(chat)[:160],
        )
        return

    title = getattr(chat, "title", None)
    if title and title != target.chat_name:
        async with acquire_connection() as conn:
            changed = await update_chat_title(conn, target.telegram_chat_id, title)
        if changed:
            stats.titles_updated += 1
            log.info(
                "membership.title_updated",
                chat_id=target.telegram_chat_id,
                old=target.chat_name,
                new=title,
            )

    async def one(account: StaffAccount) -> tuple[StaffAccount, str | None]:
        async with semaphore:
            kind, member = await _call(
                lambda: bot.get_chat_member(target.telegram_chat_id, account.telegram_user_id),
                stats,
            )
        if kind == "ok":
            return account, str(getattr(member, "status", "unknown"))
        if kind == "bad":
            # "user not found" / "participant_id_invalid": Telegram has never
            # seen this account in this chat. Equivalent to 'left' for us.
            return account, "left"
        return account, None

    results = await asyncio.gather(*(one(a) for a in accounts))
    async with acquire_connection() as conn:
        for account, status in results:
            if status is None:
                stats.errors += 1
                continue
            if is_present(status):
                stats.present += 1
            else:
                stats.absent += 1
            for unit_id in target.unit_ids:
                await record_verified_status(
                    conn,
                    chat_id=unit_id,
                    telegram_user_id=account.telegram_user_id,
                    internal_user_id=account.internal_user_id,
                    status=status,
                    verified_at=now,
                )


async def run_membership_sweep(bot: Bot, *, concurrency: int | None = None) -> SweepStats:
    """One full pass. Safe to call any time; every write is an upsert."""
    stats = SweepStats()
    async with acquire_connection() as conn:
        targets = await list_sweep_targets(conn)
        accounts = await list_staff_accounts(conn)
    if not targets or not accounts:
        log.info("membership.sweep.nothing", chats=len(targets), accounts=len(accounts))
        return stats

    semaphore = asyncio.Semaphore(concurrency or settings.MEMBERSHIP_SWEEP_CONCURRENCY)
    now = datetime.now(UTC)
    log.info("membership.sweep.start", chats=len(targets), accounts=len(accounts))
    for target in targets:
        stats.chats += 1
        try:
            await _sweep_chat(bot, target, accounts, stats, semaphore=semaphore, now=now)
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # one bad chat must not end the pass
            stats.errors += 1
            log.error(
                "membership.sweep.chat_failed",
                chat_id=target.telegram_chat_id,
                error=str(exc)[:200],
            )
    log.info(
        "membership.sweep.done",
        chats=stats.chats,
        unreachable=stats.chats_unreachable,
        titles_updated=stats.titles_updated,
        calls=stats.calls,
        present=stats.present,
        absent=stats.absent,
        errors=stats.errors,
    )
    return stats


async def membership_worker_loop(bot: Bot, interval_seconds: int | None = None) -> None:
    """Reconcile membership and titles on a schedule; first pass soon after start."""
    interval = interval_seconds or settings.MEMBERSHIP_SWEEP_INTERVAL_SECONDS
    log.info(
        "worker.membership.start",
        enabled=settings.MEMBERSHIP_SWEEP_ENABLED,
        interval_s=interval,
    )
    if not settings.MEMBERSHIP_SWEEP_ENABLED:
        return
    await asyncio.sleep(settings.MEMBERSHIP_SWEEP_STARTUP_DELAY_SECONDS)
    while True:
        try:
            await run_membership_sweep(bot)
        except asyncio.CancelledError:
            log.info("worker.membership.stop")
            raise
        except Exception as exc:  # never let one bad pass kill the loop
            log.error("worker.membership.error", error=str(exc))
        await asyncio.sleep(interval)
