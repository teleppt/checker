import asyncio
import logging
import time

from aiogram import Bot, Dispatcher
from aiogram.client.default import DefaultBotProperties
from aiogram.enums import ParseMode
from aiogram.webhook.aiohttp_server import SimpleRequestHandler, setup_application
from aiohttp import web
from sqlalchemy import select

import checker_core
import integration
import chat_captcha
import logic
from config import config
from db import ChatSettings, Session, init_db
from handlers import admin, antiflood, cleanup, gatekeeper, joins, requests, start
from dashboard import router as dashboard_router
from server import create_app

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("bot")

WEBHOOK_PATH = f"/webhook/{config.webhook_secret}"


async def raid_expiry_watcher(bot: Bot) -> None:
    """Раз в минуту: чистит протухшие капчи, снимает истёкший авто-рейд и напоминает о текущем."""
    while True:
        await asyncio.sleep(60)
        chat_captcha.cleanup_expired()

        now = int(time.time())
        async with Session() as session:
            result = await session.execute(
                select(ChatSettings).where(ChatSettings.raid_active.is_(True))
            )
            for settings in result.scalars():
                if settings.raid_expires_at and settings.raid_expires_at <= now:
                    settings.raid_active = False
                    settings.captcha_enabled = settings.captcha_before_raid
                    await logic.notify_admins(
                        bot,
                        f"Авто-режим рейда в «{logic.chat_display_name(settings)}» истёк, "
                        f"капча возвращена к прежнему состоянию.",
                        chat_id=settings.chat_id, chat_title=logic.chat_display_name(settings),
                    )
                else:
                    await logic.notify_admins(
                        bot,
                        f"⚠️ Рейд всё ещё активен в «{logic.chat_display_name(settings)}». "
                        f"Капча включена. Снять раньше — кнопка «🟢 Снять режим рейда» в карточке чата.",
                        chat_id=settings.chat_id, chat_title=logic.chat_display_name(settings),
                    )
                    settings.raid_last_reminder_at = now
            await session.commit()


async def main() -> None:
    await init_db()

    bot = Bot(
        token=config.bot_token,
        default=DefaultBotProperties(parse_mode=ParseMode.HTML),
    )
    checker_core.init(bot)

    dp = Dispatcher()
    # ВАЖНО: порядок подключения важен для chat_member-событий (вступление/выход).
    # aiogram по умолчанию обрабатывает апдейт только ПЕРВЫМ подошедшим хендлером
    # среди всех роутеров — joins.router должен идти раньше checker_core.router,
    # иначе безусловный @router.chat_member() в checker_core перехватит вступления/
    # выходы первым и anti-raid часть (капча, notify_admins) вообще не отработает.
    # Обновление кэша живого членства checker'а из join/leave вызывается явно
    # изнутри joins.py — checker_core.router ловит только остальные chat_member
    # события (повышение/понижение прав и т.п.), см. checker_core.py.
    dp.include_router(joins.router)
    dp.include_router(checker_core.router)
    dp.include_router(chat_captcha.router)
    dp.include_router(start.router)
    dp.include_router(dashboard_router)
    dp.include_router(admin.router)
    dp.include_router(cleanup.router)
    dp.include_router(requests.router)
    dp.include_router(antiflood.router)
    dp.include_router(gatekeeper.router)

    async def _on_startup(**kwargs) -> None:
        asyncio.create_task(raid_expiry_watcher(bot))
        asyncio.create_task(checker_core.log_sender_worker())
        asyncio.create_task(checker_core.error_summary_worker())
        asyncio.create_task(checker_core.cleanup_worker())
        if checker_core.redis_client:
            try:
                await checker_core.redis_client.ping()
                logger.info("Checker: Redis подключён, состояние переживёт рестарты")
            except Exception as e:
                logger.warning(f"Checker: CHECKER_REDIS_URL задан, но подключиться не удалось: {e}. Работаю в памяти.")
        else:
            logger.info("Checker: CHECKER_REDIS_URL не задан — состояние checker'а живёт только в памяти")

    dp.startup.register(_on_startup)

    app = create_app(bot)
    app.add_routes(checker_core.routes)  # /checker/health, /checker/stats, /checker/check, ...
    app.add_routes(integration.routes)  # /checker/integration/* — мост game-main <-> rp-main
    SimpleRequestHandler(dispatcher=dp, bot=bot, secret_token=config.webhook_secret).register(app, path=WEBHOOK_PATH)
    setup_application(app, dp, bot=bot)

    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, "0.0.0.0", config.port)
    await site.start()
    logger.info(
        "Server started on 0.0.0.0:%s (webhook at %s, checker API at /checker/*)",
        config.port, WEBHOOK_PATH,
    )

    webhook_url = f"{config.webapp_url}{WEBHOOK_PATH}"
    await bot.set_webhook(
        url=webhook_url,
        secret_token=config.webhook_secret,
        drop_pending_updates=False,  # живое членство (checker) не должно теряться при рестарте
        allowed_updates=[
            "message", "edited_message", "chat_member", "my_chat_member",
            "callback_query", "chat_join_request",
        ],
    )
    logger.info("Webhook set to %s", webhook_url)

    if config.topics_owner_id:
        logger.info(
            "Темы логов создаются в личке с владельцем (id=%s). Если темы не появляются — "
            "проверь в @BotFather, что для бота включены Topics в приватных чатах.",
            config.topics_owner_id,
        )

    await asyncio.Event().wait()  # держим процесс живым — обновления теперь приходят через вебхук


if __name__ == "__main__":
    asyncio.run(main())
