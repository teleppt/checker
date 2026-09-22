from __future__ import annotations

import logging

import topics
from config import config
from db import Session, get_or_create_settings
from state import join_tracker

logger = logging.getLogger("logic")




def is_admin(user_id: int | None) -> bool:
    return user_id is not None and user_id in config.admin_ids


async def set_captcha(chat_id: int, enabled: bool) -> None:
    async with Session() as session:
        settings = await get_or_create_settings(session, chat_id)
        settings.captcha_enabled = enabled
        settings.captcha_manual = True
        settings.raid_active = False
        await session.commit()


async def set_captcha_auto(chat_id: int) -> None:
    async with Session() as session:
        settings = await get_or_create_settings(session, chat_id)
        settings.captcha_manual = False
        await session.commit()


async def turn_off_raid(chat_id: int) -> bool:
    """Возвращает True, если режим рейда был активен и его сняли."""
    async with Session() as session:
        settings = await get_or_create_settings(session, chat_id)
        if not settings.raid_active:
            return False
        settings.raid_active = False
        settings.captcha_enabled = settings.captcha_before_raid
        await session.commit()
        return True


async def set_join_blocked(chat_id: int, blocked: bool) -> None:
    async with Session() as session:
        settings = await get_or_create_settings(session, chat_id)
        settings.join_blocked = blocked
        await session.commit()


async def set_notify_subs(chat_id: int, enabled: bool) -> None:
    async with Session() as session:
        settings = await get_or_create_settings(session, chat_id)
        settings.notify_subs = enabled
        await session.commit()


async def set_antiflood(chat_id: int, enabled: bool) -> None:
    async with Session() as session:
        settings = await get_or_create_settings(session, chat_id)
        settings.antiflood_enabled = enabled
        await session.commit()


def status_emoji(chat_type: str, settings) -> str:
    if settings.join_blocked:
        return "⛔️"
    if settings.raid_active:
        return "⚠️"
    if chat_type == "group":
        return "✅" if settings.captcha_enabled else "❌"
    return "✅"


def chat_display_name(settings) -> str:
    if settings.username:
        return f"@{settings.username}"
    return settings.title or str(settings.chat_id)


async def notify_admins(bot, text: str, *, chat_id: int | None = None, chat_title: str | None = None) -> None:
    """Шлёт лог всем ADMIN_IDS.

    Владельцу (config.topics_owner_id) сообщение уходит в личку с разбивкой по темам:
    если передан chat_id (событие относится к конкретному каналу/группе — подписка/
    отписка, бан, флуд, рейд...) — в тему этого чата (создаётся автоматически при
    первом таком событии). Без chat_id — в "общую" ленту (без темы).
    Остальным админам из ADMIN_IDS (если их несколько) — тем же текстом, но плоско,
    без тем: темы поддерживаются только в личке одного конкретного пользователя.
    """
    logger.info("notify_admins: chat_id=%s admin_ids=%s topics_owner_id=%s", chat_id, config.admin_ids, config.topics_owner_id)

    thread_id = None
    if chat_id is not None and config.topics_owner_id is not None:
        title = chat_title
        if title is None:
            try:
                settings = await get_settings_snapshot(chat_id)
                title = chat_display_name(settings)
            except Exception:
                title = str(chat_id)
        thread_id = await topics.get_channel_topic(bot, config.topics_owner_id, str(chat_id), title)
        logger.info("notify_admins: тема канала %s -> thread_id=%s", chat_id, thread_id)

    if not config.admin_ids:
        logger.warning("notify_admins: ADMIN_IDS пуст, слать некому")
        return

    for admin_id in config.admin_ids:
        try:
            if admin_id == config.topics_owner_id and thread_id is not None:
                await bot.send_message(admin_id, text, message_thread_id=thread_id)
            else:
                await bot.send_message(admin_id, text)
            logger.info("notify_admins: отправлено админу %s (thread_id=%s)", admin_id, thread_id if admin_id == config.topics_owner_id else None)
        except Exception:
            logger.warning("Не удалось отправить лог админу %s", admin_id, exc_info=True)


async def status_text(chat_id: int) -> str:
    async with Session() as session:
        settings = await get_or_create_settings(session, chat_id)

    count = join_tracker.count(chat_id, config.raid_window_seconds)
    return "\n".join([
        f"Капча: {'включена' if settings.captcha_enabled else 'выключена'}"
        f" ({'вручную' if settings.captcha_manual else 'авто'})",
        f"Режим рейда: {'АКТИВЕН 🔴' if settings.raid_active else 'спокойно 🟢'}",
        f"Вступлений за последние {config.raid_window_seconds} сек: {count}"
        f" (порог тревоги: {config.raid_join_threshold})",
    ])


async def get_settings_snapshot(chat_id: int):
    async with Session() as session:
        return await get_or_create_settings(session, chat_id)
