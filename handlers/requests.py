from __future__ import annotations

import asyncio
import time

from aiogram import Bot, Router
from aiogram.exceptions import TelegramBadRequest, TelegramForbiddenError
from aiogram.types import ChatJoinRequest

import cas
import chat_captcha
import db
import extreme_raid
import logic
import media
from config import config
from db import Session, get_or_create_settings
from state import decline_tasks, join_tracker, verified_requests

router = Router(name="requests")


def _cancel_decline_task(key: tuple[int, int]) -> None:
    task = decline_tasks.pop(key, None)
    if task:
        task.cancel()


async def _auto_decline(bot: Bot, chat_id: int, user_id: int) -> None:
    try:
        await asyncio.sleep(config.request_decline_seconds)
    except asyncio.CancelledError:
        return
    decline_tasks.pop((chat_id, user_id), None)
    try:
        await bot.decline_chat_join_request(chat_id, user_id)
    except TelegramBadRequest:
        pass  # заявки уже нет — и хорошо


@router.chat_join_request()
async def on_join_request(event: ChatJoinRequest, bot: Bot) -> None:
    chat_id = event.chat.id
    user = event.from_user

    await db.upsert_chat_info(chat_id, "channel", event.chat.title or "", event.chat.username or "")

    # Доверенный пользователь — одобряем сразу, без капчи и проверок
    if await db.is_trusted(user.id):
        try:
            await bot.approve_chat_join_request(chat_id, user.id)
        except TelegramBadRequest:
            pass
        return

    # Проверка по базе известных спам-ботов CAS — отклоняем сразу, до капчи
    if await cas.is_banned(user.id):
        try:
            await bot.decline_chat_join_request(chat_id, user.id)
        except TelegramBadRequest:
            pass
        await db.record_ban(chat_id, user.id, user.username or "", user.first_name or "", "CAS", "в базе CAS")
        await db.add_audit(chat_id, 0, "cas_ban", f"user={user.id}")
        await logic.notify_admins(
            bot, f"⛔️ Отклонил заявку {user.mention_html()} в «{event.chat.title}» — числится в базе CAS.",
            chat_id=chat_id, chat_title=event.chat.title,
        )
        return

    async with Session() as session:
        settings = await get_or_create_settings(session, chat_id)

        if settings.join_blocked:
            await session.commit()
            try:
                await bot.decline_chat_join_request(chat_id, user.id)
            except TelegramBadRequest:
                pass
            return

        count_in_window = join_tracker.register_join(chat_id, config.raid_window_seconds)

        if (
            count_in_window >= config.raid_join_threshold
            and not settings.raid_active
            and not settings.captcha_manual
        ):
            settings.captcha_before_raid = settings.captcha_enabled
            settings.captcha_enabled = True
            settings.raid_active = True
            settings.raid_expires_at = int(time.time()) + config.raid_captcha_duration
            await session.commit()
            await logic.notify_admins(
                bot,
                f"⚠️ Похоже на накрутку заявок в «{logic.chat_display_name(settings)}»: "
                f"{count_in_window} заявок за {config.raid_window_seconds} сек.\n"
                f"Включил капчу для новых заявок на {config.raid_captcha_duration // 60} мин.",
                chat_id=chat_id, chat_title=logic.chat_display_name(settings),
            )

        captcha_needed = settings.captcha_enabled

    key = (chat_id, user.id)

    if not captcha_needed:
        try:
            await bot.approve_chat_join_request(chat_id, user.id)
        except TelegramBadRequest:
            pass
        return

    if key in verified_requests:
        verified_requests.discard(key)
        try:
            await bot.approve_chat_join_request(chat_id, user.id)
        except TelegramBadRequest:
            pass
        return

    async def _on_success(bot: Bot, session: chat_captcha.CaptchaSession) -> None:
        task = decline_tasks.pop(key, None)
        if task:
            task.cancel()
        try:
            await bot.approve_chat_join_request(chat_id, user.id)
            await db.record_member_join(chat_id, user.id, user.username or "", user.first_name or "", int(time.time()))
        except TelegramBadRequest:
            verified_requests.add(key)
        if session.message_id:
            try:
                await bot.delete_message(user.id, session.message_id)
            except TelegramBadRequest:
                pass
        try:
            await bot.send_message(user.id, "✅ Проверка пройдена, заявка одобрена — добро пожаловать!")
        except TelegramBadRequest:
            pass

    async def _on_timeout(bot: Bot, session: chat_captcha.CaptchaSession) -> None:
        if session.message_id:
            try:
                await bot.delete_message(user.id, session.message_id)
            except TelegramBadRequest:
                pass
        # Заявка сама авто-отклонится по отдельному таймеру (_auto_decline,
        # config.request_decline_seconds) — тут просто убираем сообщение с капчей.

    session = chat_captcha.create_session(
        bot, user.id, user.id, flow="channel_request", target_chat_id=chat_id,
        on_success=_on_success, on_timeout=_on_timeout,
    )
    caption = (
        f"Привет! В канале «{event.chat.title}» сейчас включена проверка заявок на вступление "
        f"(защита от накрутки). Пройди капчу ниже — заявку одобрю сразу после этого.\n\n"
        + chat_captcha.caption_text(user.mention_html())
    )

    try:
        msg = await media.send_animation_with_fallback(
            bot, user.id, media.CHAT_VIDEO,
            captions=[caption],
            keyboards=[chat_captcha.build_keyboard(session)],
        )
        if msg:
            session.message_id = msg.message_id
    except TelegramForbiddenError:
        chat_captcha.sessions.pop(session.token, None)
        if session.task:
            session.task.cancel()
        # Пользователь не запускал бота — Telegram не даёт написать первым.
        # Заявка авто-отклонится по таймауту (см. ниже), админам подсказываем
        # использовать персональную ссылку бота вместо обычной ссылки на канал.
        await logic.notify_admins(
            bot,
            f"Не смог написать пользователю {user.id} (@{user.username}) "
            f"для капчи в канал {chat_id} — он не запускал бота.\n"
            f"Заявка авто-отклонится через {config.request_decline_seconds // 60} мин.\n"
            f"Совет: разошли вместо обычной ссылки персональную ссылку бота "
            f"(кнопка «🔗 Ссылка для входа через бота» в карточке этого канала — /start → Каналы).",
            chat_id=chat_id, chat_title=event.chat.title,
        )

    _cancel_decline_task(key)
    decline_tasks[key] = asyncio.create_task(_auto_decline(bot, chat_id, user.id))

    await extreme_raid.handle_channel_request_burst(bot, chat_id)
