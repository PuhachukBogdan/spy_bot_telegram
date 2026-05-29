"""aiogram Bot + Dispatcher. Phase 3.

Module-level singletons. Handlers register against ``dp``; ``main.py`` uses
``bot`` to set the webhook on startup and feeds updates into ``dp``.

Importing this module yields a fully wired dispatcher: the DM-command router is
included and the audit + whitelist middlewares are registered, so ``main.py``
only has to call ``dp.feed_update(bot, update)``.
"""

from __future__ import annotations

from aiogram import Bot, Dispatcher
from aiogram.client.default import DefaultBotProperties
from aiogram.enums import ParseMode

from src.bot.handlers.dm_commands import router as dm_commands_router
from src.bot.middleware.audit import AuditMiddleware
from src.bot.middleware.whitelist import WhitelistMiddleware
from src.config import settings

bot = Bot(
    token=settings.TELEGRAM_BOT_TOKEN.get_secret_value(),
    default=DefaultBotProperties(parse_mode=ParseMode.HTML),
)

dp = Dispatcher()

# Outer middlewares run for every message before routing. Audit first so a
# command is logged even if the whitelist later drops the message; whitelist
# second so non-active group messages never reach a handler.
dp.message.outer_middleware(AuditMiddleware())
dp.message.outer_middleware(WhitelistMiddleware())

dp.include_router(dm_commands_router)
