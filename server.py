from __future__ import annotations

from aiogram import Bot
from aiohttp import web

# Веб-версия капчи (Mini App) убрана — см. chat_captcha.py и MERGE_NOTES.md.
# Этот модуль теперь только создаёт голый aiohttp-app: вебхук бота (см. bot.py)
# и HTTP API checker'а (см. checker_core.py) вешаются на него отдельно.


async def _health(request: web.Request) -> web.Response:
    return web.json_response({"status": "ok"})


def create_app(bot: Bot) -> web.Application:
    app = web.Application()
    app["bot"] = bot
    app.router.add_get("/", _health)
    return app
