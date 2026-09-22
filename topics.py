"""
Единая система логов через темы (Topics) в личке владельца с ботом.

С недавнего обновления Bot API createForumTopic можно вызывать не только в
форум-супергруппе, но и в приватном чате с пользователем — тогда никакая
отдельная группа не нужна: темы создаются прямо в ЛС между владельцем и ботом.

⚠️ Предварительное условие: у владельца бота должны быть включены Topics в
личке с этим ботом (@BotFather -> выбрать бота -> Bot Settings -> см. пункт
про темы/Direct Messages в приватных чатах). Если это не включено,
create_forum_topic вернёт ошибку — тогда мы один раз логируем предупреждение
и просто шлём сообщение без темы (в "общую" ленту), чтобы логи не терялись.

Два вида тем:
- kind="bot"     — тема на клиент-бота. Создаётся при первом обращении этого
                    bot_name к checker'у ("регистрация" бота). Общие/системные
                    логи по этому боту падают сюда.
- kind="channel" — тема на канал/группу. Создаётся при первом относящемся к
                    этому чату событии (подписка/отписка, бан, флуд, рейд...).
                    Все подобные логи для этого канала/группы падают сюда,
                    независимо от того, какой встроенный модуль их прислал.
"""
from __future__ import annotations

import asyncio
import logging

from aiogram import Bot
from aiogram.exceptions import TelegramBadRequest, TelegramForbiddenError

import db

logger = logging.getLogger("topics")

# Цвета иконок тем (см. ограниченный набор в Bot API)
_COLOR_BOT = 0x6FB9F0
_COLOR_CHANNEL = 0xFFD67E

# (kind, key) -> thread_id — кэш поверх БД, чтобы не ходить в SQLite на каждый лог
_cache: dict[tuple[str, str], int] = {}

# Чтобы два параллельных лога по одному и тому же новому bot_name/каналу не создали
# в Telegram две разные темы одновременно (оба видят пустой кэш и оба идут создавать) —
# сериализуем создание темы по конкретному ключу.
_creation_locks: dict[tuple[str, str], asyncio.Lock] = {}

# Чтобы не спамить одним и тем же предупреждением "темы не включены" при каждом логе
_warned_disabled = False


def _norm_bot_key(bot_name: str) -> str:
    return (bot_name or "unknown").strip().lower()[:64] or "unknown"


async def _get_cached(kind: str, key: str) -> int | None:
    cache_key = (kind, key)
    if cache_key in _cache:
        return _cache[cache_key]
    thread_id = await db.get_topic_thread(kind, key)
    if thread_id is not None:
        _cache[cache_key] = thread_id
    return thread_id


async def _create_topic(bot: Bot, admin_id: int, kind: str, key: str, name: str, color: int) -> int | None:
    global _warned_disabled
    logger.info("Создаю тему %r (kind=%s, key=%s) в личке %s", name, kind, key, admin_id)
    try:
        topic = await bot.create_forum_topic(chat_id=admin_id, name=name[:128], icon_color=color)
    except (TelegramBadRequest, TelegramForbiddenError) as e:
        if not _warned_disabled:
            _warned_disabled = True
            logger.warning(
                "Не удалось создать тему %r в личке с владельцем (%s). "
                "Проверь в @BotFather, что для бота включены Topics в приватных чатах. "
                "Логи пока идут без разбивки на темы.",
                name, e,
            )
        return None
    thread_id = topic.message_thread_id
    logger.info("Тема %r создана, thread_id=%s", name, thread_id)
    await db.save_topic_thread(kind, key, thread_id, name)
    _cache[(kind, key)] = thread_id
    return thread_id


async def _get_or_create(bot: Bot, admin_id: int, kind: str, key: str, name: str, color: int) -> int | None:
    thread_id = await _get_cached(kind, key)
    if thread_id is not None:
        return thread_id
    lock = _creation_locks.setdefault((kind, key), asyncio.Lock())
    async with lock:
        # Пока ждали лок, кто-то другой мог уже создать тему — перепроверяем.
        thread_id = await _get_cached(kind, key)
        if thread_id is not None:
            return thread_id
        return await _create_topic(bot, admin_id, kind, key, name, color)


async def get_bot_topic(bot: Bot, admin_id: int, bot_name: str) -> int | None:
    """Тема клиент-бота. Первый вызов для нового bot_name = 'регистрация' — тема
    создаётся автоматически и дальше переиспользуется."""
    key = _norm_bot_key(bot_name)
    return await _get_or_create(bot, admin_id, "bot", key, f"🤖 {bot_name}", _COLOR_BOT)


async def get_channel_topic(bot: Bot, admin_id: int, channel_key: str, display_name: str) -> int | None:
    """Тема канала/группы. channel_key — стабильный ключ (chat_id строкой, либо
    нормализованный @username канала). display_name — то, что видно в названии
    темы (username/title канала)."""
    key = str(channel_key)
    return await _get_or_create(bot, admin_id, "channel", key, f"📢 {display_name}", _COLOR_CHANNEL)
