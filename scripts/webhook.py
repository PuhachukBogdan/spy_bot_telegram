"""Telegram webhook management — dev utility (set / info / delete).

Reads all config from ``.env`` via ``src.config.settings``. Never prints secret
values: the bot token and webhook secret are read through ``SecretStr`` and only
passed to the Telegram API, never logged. ``getWebhookInfo`` does not return the
secret token, so ``info`` output is safe to share.

Run from the project root, inside the venv, as a module (so ``import src`` works):

    python -m scripts.webhook set      # register webhook (URL + secret from .env)
    python -m scripts.webhook info     # show getWebhookInfo (no secrets)
    python -m scripts.webhook delete   # remove webhook (cleanup)

Note: ``src/main.py`` already calls ``set_webhook`` on startup, so restarting
uvicorn after editing ``TELEGRAM_WEBHOOK_URL`` also re-registers. This script is
for inspecting or clearing the webhook without restarting the server.
"""

from __future__ import annotations

import asyncio
import sys

from aiogram import Bot

from src.config import settings

_ACTIONS = ("set", "info", "delete")


async def _set(bot: Bot) -> None:
    url = settings.TELEGRAM_WEBHOOK_URL
    await bot.set_webhook(
        url=url,
        secret_token=settings.TELEGRAM_WEBHOOK_SECRET.get_secret_value(),
        drop_pending_updates=False,
    )
    print(f"setWebhook OK -> {url}")


async def _info(bot: Bot) -> None:
    info = await bot.get_webhook_info()
    print("getWebhookInfo:")
    print(f"  url:                  {info.url or '(none)'}")
    print(f"  pending_update_count: {info.pending_update_count}")
    print(f"  ip_address:           {info.ip_address or '-'}")
    print(f"  last_error_date:      {info.last_error_date or '-'}")
    print(f"  last_error_message:   {info.last_error_message or '-'}")
    print(f"  max_connections:      {info.max_connections}")
    print(f"  allowed_updates:      {info.allowed_updates or '(default)'}")


async def _delete(bot: Bot) -> None:
    await bot.delete_webhook(drop_pending_updates=False)
    print("deleteWebhook OK")


async def main(action: str) -> int:
    if action not in _ACTIONS:
        print(f"unknown action {action!r}; use one of: {', '.join(_ACTIONS)}")
        return 2
    bot = Bot(token=settings.TELEGRAM_BOT_TOKEN.get_secret_value())
    try:
        await {"set": _set, "info": _info, "delete": _delete}[action](bot)
    finally:
        await bot.session.close()
    return 0


if __name__ == "__main__":
    arg = sys.argv[1] if len(sys.argv) > 1 else "info"
    raise SystemExit(asyncio.run(main(arg)))
