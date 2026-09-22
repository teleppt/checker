"""
Пример кода для ОСНОВНОГО бота (который находится в другом Railway-проекте).
Этот файл нужно скопировать (или просто переиспользовать функции) в проект основного бота.
"""

import os
import asyncio
import logging
import aiohttp

logger = logging.getLogger("checker_client")

CHECKER_URL = os.environ.get("CHECKER_URL")  # например: https://checker-production.up.railway.app
CHECKER_API_KEY = os.environ.get("CHECKER_API_KEY")

# Название этого бота — будет видно в логах у админа, чтобы понимать какой бот прислал запрос
BOT_NAME = os.environ.get("BOT_NAME", "MyBot")

_REQUEST_TIMEOUT = aiohttp.ClientTimeout(total=10)

# Переиспользуемая сессия вместо создания новой на каждый запрос — экономит
# соединения/DNS-резолвы. Создаётся лениво при первом использовании.
_session: "aiohttp.ClientSession | None" = None


async def _get_session() -> aiohttp.ClientSession:
    global _session
    if _session is None or _session.closed:
        _session = aiohttp.ClientSession(timeout=_REQUEST_TIMEOUT)
    return _session


async def close_session() -> None:
    """Вызвать при остановке бота (например, в on_shutdown), чтобы закрыть
    сессию корректно и не оставлять предупреждение 'Unclosed client session'."""
    global _session
    if _session is not None and not _session.closed:
        await _session.close()


async def is_subscribed(
    user_id: int,
    channel: str,
    username: str | None = None,
    notify: bool = True,
    fail_open: bool = False,
) -> bool:
    """Проверка подписки на один канал.

    notify=True — этот вызов попадёт в лог админу (используй для /start).
    notify=False — тихая проверка, без уведомления (для прочих действий в боте).
    fail_open=False (по умолчанию) — если checker-сервис недоступен/упал/ответил с
        ошибкой, считаем юзера НЕ подписанным (безопасный дефолт — не пропускаем
        мимо проверки). Поставь fail_open=True, если для твоего бота важнее не
        блокировать пользователей при сбое checker'а, чем гарантировать подписку.
    """
    if not CHECKER_URL or not CHECKER_API_KEY:
        logger.error("CHECKER_URL/CHECKER_API_KEY не заданы — проверка подписки невозможна")
        return fail_open

    try:
        session = await _get_session()
        async with session.get(
            f"{CHECKER_URL}/checker/check",
            params={
                "user_id": user_id,
                "channel": channel,
                "bot_name": BOT_NAME,
                "username": username or "",
                "notify": str(notify).lower(),
            },
            headers={"X-API-Key": CHECKER_API_KEY},
        ) as resp:
            if resp.status != 200:
                logger.warning(f"Checker вернул статус {resp.status} для user_id={user_id}, channel={channel}")
                return fail_open
            data = await resp.json()
            return data.get("subscribed", fail_open)

    except (aiohttp.ClientError, asyncio.TimeoutError) as e:
        logger.warning(f"Checker недоступен ({e.__class__.__name__}: {e}) — считаем как fail_open={fail_open}")
        return fail_open


async def is_subscribed_to_all(
    user_id: int,
    channels: list[str],
    username: str | None = None,
    notify: bool = True,
    fail_open: bool = False,
) -> bool:
    """Проверка подписки сразу на несколько каналов одним запросом.

    notify / fail_open — см. docstring is_subscribed.
    """
    if not CHECKER_URL or not CHECKER_API_KEY:
        logger.error("CHECKER_URL/CHECKER_API_KEY не заданы — проверка подписки невозможна")
        return fail_open

    try:
        session = await _get_session()
        async with session.get(
            f"{CHECKER_URL}/checker/check_many",
            params={
                "user_id": user_id,
                "channels": ",".join(channels),
                "bot_name": BOT_NAME,
                "username": username or "",
                "notify": str(notify).lower(),
            },
            headers={"X-API-Key": CHECKER_API_KEY},
        ) as resp:
            if resp.status != 200:
                logger.warning(f"Checker вернул статус {resp.status} для user_id={user_id}, channels={channels}")
                return fail_open
            data = await resp.json()
            return data.get("all_subscribed", fail_open)

    except (aiohttp.ClientError, asyncio.TimeoutError) as e:
        logger.warning(f"Checker недоступен ({e.__class__.__name__}: {e}) — считаем как fail_open={fail_open}")
        return fail_open


# Пример использования внутри хэндлера aiogram:
#
# @router.message(Command("start"))
# async def start_handler(message: types.Message):
#     # notify по умолчанию True — этот вызов уйдёт в лог админу
#     ok = await is_subscribed_to_all(
#         message.from_user.id,
#         ["@channel1", "@channel2"],
#         username=message.from_user.username,
#     )
#     if not ok:
#         await message.answer("Сначала подпишись на все каналы!")
#         return
#     await message.answer("Добро пожаловать!")
#
#
# @router.callback_query(F.data == "some_button")
# async def some_button_handler(callback: types.CallbackQuery):
#     # notify=False — тихая проверка, не /start, в лог админу не попадёт
#     ok = await is_subscribed_to_all(
#         callback.from_user.id,
#         ["@channel1", "@channel2"],
#         notify=False,
#     )
#     if not ok:
#         await callback.answer("Сначала подпишись на каналы!", show_alert=True)
#         return
#     # ... остальная логика
#
#
# # В on_shutdown основного бота (если он определён):
# # from client_example import close_session
# # await close_session()
