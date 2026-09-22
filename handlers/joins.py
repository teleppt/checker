from __future__ import annotations

import asyncio
import logging
import time

from aiogram import Bot, Router
from aiogram.exceptions import TelegramBadRequest
from aiogram.filters import JOIN_TRANSITION, LEAVE_TRANSITION, ChatMemberUpdatedFilter
from aiogram.types import ChatMemberUpdated, ChatPermissions

import cas
import chat_captcha
import checker_core
import db
import extreme_raid
import heuristics
import logic
import media
import premium_emoji
from config import config
from db import Session, get_or_create_settings
from state import join_tracker, recent_joiners

router = Router(name="joins")
logger = logging.getLogger("joins")

FULL_PERMISSIONS = ChatPermissions(
    can_send_messages=True,
    can_send_audios=True,
    can_send_documents=True,
    can_send_photos=True,
    can_send_videos=True,
    can_send_video_notes=True,
    can_send_voice_notes=True,
    can_send_polls=True,
    can_send_other_messages=True,
    can_add_web_page_previews=True,
)


async def _send_welcome(bot: Bot, chat_id: int, user) -> None:
    """Приветствие с видео для тех, кому капча не показывается вообще (выключена
    для чата или доверенный пользователь) — чтобы вместо тишины было как приветствие
    при обычном прохождении проверки."""
    mention = user.mention_html()
    await media.send_animation_with_fallback(
        bot, chat_id, media.PREW_VIDEO,
        captions=[
            f"{premium_emoji.tag(premium_emoji.WAVE, '👋')} {mention}, добро пожаловать в чат!",
            f"👋 {mention}, добро пожаловать в чат!",
        ],
    )


def _chat_type_label(tg_type: str) -> str:
    if tg_type in ("group", "supergroup"):
        return "group"
    if tg_type == "channel":
        return "channel"
    return "other"


async def _restrict(bot: Bot, chat_id: int, user_id: int) -> None:
    try:
        await bot.restrict_chat_member(
            chat_id, user_id, permissions=ChatPermissions(can_send_messages=False)
        )
    except TelegramBadRequest:
        pass  # например, уже админ — ограничить нельзя, и не нужно


async def _kick(bot: Bot, chat_id: int, user_id: int) -> None:
    try:
        await bot.ban_chat_member(chat_id, user_id)
        await bot.unban_chat_member(chat_id, user_id, only_if_banned=True)
    except TelegramBadRequest:
        pass


# ---------- бот сам добавлен/повышен/удалён — регистрируем чат сразу ----------

@router.my_chat_member()
async def on_bot_status_change(event: ChatMemberUpdated) -> None:
    chat_type = _chat_type_label(event.chat.type)
    await db.upsert_chat_info(event.chat.id, chat_type, event.chat.title or "", event.chat.username or "")


# ---------- обычный участник вступил ----------

@router.chat_member(ChatMemberUpdatedFilter(member_status_changed=JOIN_TRANSITION))
async def on_member_join(event: ChatMemberUpdated, bot: Bot) -> None:
    chat_id = event.chat.id
    user = event.new_chat_member.user
    chat_type = _chat_type_label(event.chat.type)
    logger.info("on_member_join: chat_id=%s user_id=%s chat_type=%s", chat_id, user.id, chat_type)

    await checker_core.update_membership_from_event(event)

    await db.upsert_chat_info(chat_id, chat_type, event.chat.title or "", event.chat.username or "")

    if user.is_bot:
        return

    settings_now = await logic.get_settings_snapshot(chat_id)

    # Вход закрыт всем — кикаем не глядя на капчу
    if settings_now.join_blocked:
        await _kick(bot, chat_id, user.id)
        return

    # Доверенный пользователь — пускаем сразу, без капчи и проверок
    if await db.is_trusted(user.id):
        await db.record_member_join(chat_id, user.id, user.username or "", user.first_name or "", int(time.time()))
        if settings_now.notify_subs:
            await logic.notify_admins(bot, f"➕ {user.mention_html()} (доверенный) в «{logic.chat_display_name(settings_now)}»", chat_id=chat_id, chat_title=logic.chat_display_name(settings_now))
        if chat_type == "group":
            await _send_welcome(bot, chat_id, user)
        return

    # Проверка по базе известных спам-ботов CAS — баним сразу, до капчи
    if await cas.is_banned(user.id):
        await _kick(bot, chat_id, user.id)
        await db.record_ban(chat_id, user.id, user.username or "", user.first_name or "", "CAS", "в базе CAS")
        await db.add_audit(chat_id, 0, "cas_ban", f"user={user.id}")
        await logic.notify_admins(
            bot, f"⛔️ {user.mention_html()} забанен в «{logic.chat_display_name(settings_now)}» — числится в базе CAS.",
            chat_id=chat_id, chat_title=logic.chat_display_name(settings_now),
        )
        return

    # Регистрируем в памяти СРАЗУ, ещё до записи в БД — при настоящем рейде (сотни
    # вступлений за секунды) запись в SQLite может не успевать, а от этого трекера
    # зависит, кого забанит extreme_raid при экстренном срабатывании.
    recent_joiners.register(chat_id, user.id, user.username or "", user.first_name or "")
    await db.record_member_join(chat_id, user.id, user.username or "", user.first_name or "", int(time.time()))

    if settings_now.notify_subs:
        tag = f" (@{user.username})" if user.username else ""
        await logic.notify_admins(bot, f"➕ Новый участник в «{logic.chat_display_name(settings_now)}»: {user.mention_html()}{tag}", chat_id=chat_id, chat_title=logic.chat_display_name(settings_now))

    async with Session() as session:
        settings = await get_or_create_settings(session, chat_id)
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
                f"⚠️ Похоже на накрутку в «{logic.chat_display_name(settings_now)}»: "
                f"{count_in_window} вступлений за {config.raid_window_seconds} сек.\n"
                f"Автоматически включил капчу на {config.raid_captcha_duration // 60} мин.",
                chat_id=chat_id, chat_title=logic.chat_display_name(settings_now),
            )

        captcha_needed = settings.captcha_enabled

    # Экстренный авто-бан всплеска — проверяем и для каналов, и для групп. Раньше эта
    # проверка стояла ПОСЛЕ отсечки "if chat_type != group: return" и для каналов вообще
    # никогда не выполнялась — если рейд шёл прямыми подписками на канал (без заявок на
    # вступление), извлечение и авто-бан просто не запускались.
    if await extreme_raid.handle_group_burst(bot, chat_id):
        return  # этого (и всех недавних) уже банит extreme_raid

    if chat_type != "group":
        return  # в канале капча уже прошла на этапе заявки/ссылки — тут больше нечего делать

    # Даже если капча выключена глобально — подозрительным (шаблонный юзернейм + нет фото)
    # всё равно покажем капчу выборочно
    if not captcha_needed and await heuristics.is_suspicious(bot, user):
        captcha_needed = True

    if not captcha_needed:
        await _send_welcome(bot, chat_id, user)
        return

    await _restrict(bot, chat_id, user.id)

    async def _on_success(bot: Bot, session: chat_captcha.CaptchaSession) -> None:
        try:
            await bot.restrict_chat_member(chat_id, user.id, permissions=FULL_PERMISSIONS)
        except TelegramBadRequest:
            pass
        if session.message_id:
            try:
                await bot.delete_message(chat_id, session.message_id)
            except TelegramBadRequest:
                pass
        await _send_welcome(bot, chat_id, user)

    async def _on_timeout(bot: Bot, session: chat_captcha.CaptchaSession) -> None:
        await _kick(bot, chat_id, user.id)
        await db.record_member_leave(chat_id, user.id)
        if session.message_id:
            try:
                await bot.delete_message(chat_id, session.message_id)
            except TelegramBadRequest:
                pass

    session = chat_captcha.create_session(
        bot, user.id, chat_id, flow="group", target_chat_id=chat_id,
        on_success=_on_success, on_timeout=_on_timeout,
    )
    msg = await media.send_animation_with_fallback(
        bot, chat_id, media.CHAT_VIDEO,
        captions=[chat_captcha.caption_text(user.mention_html())],
        keyboards=[chat_captcha.build_keyboard(session)],
    )
    if msg:
        session.message_id = msg.message_id


# ---------- участник вышел/был удалён ----------

@router.chat_member(ChatMemberUpdatedFilter(member_status_changed=LEAVE_TRANSITION))
async def on_member_leave(event: ChatMemberUpdated, bot: Bot) -> None:
    chat_id = event.chat.id
    user = event.old_chat_member.user
    if user.is_bot:
        return

    await db.record_member_leave(chat_id, user.id)
    await checker_core.update_membership_from_event(event)

    settings = await logic.get_settings_snapshot(chat_id)
    if settings.notify_subs:
        tag = f"@{user.username}" if user.username else (user.first_name or str(user.id))
        await logic.notify_admins(bot, f"➖ {tag} покинул(а) «{logic.chat_display_name(settings)}».", chat_id=chat_id, chat_title=logic.chat_display_name(settings))
