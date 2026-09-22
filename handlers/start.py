from __future__ import annotations

from aiogram import Bot, Router
from aiogram.enums import ChatType
from aiogram.exceptions import TelegramBadRequest
from aiogram.filters import Command, CommandObject
from aiogram.types import Message

import cas
import chat_captcha
import dashboard
import db
import logic
import media
from config import config

router = Router(name="start")

# Канальная капча (личная ссылка на канал) — токен-чат создаём на лету по chat_id.
# Групповая капча теперь показывается сразу в группе (см. handlers/joins.py),
# отдельный переход в ЛС бота ей больше не нужен.
JOIN_PAYLOAD_PREFIX = "join_"


@router.message(Command("start"))
async def cmd_start(message: Message, bot: Bot, command: CommandObject) -> None:
    payload = (command.args or "").strip()

    if message.chat.type == ChatType.PRIVATE and payload.startswith(JOIN_PAYLOAD_PREFIX):
        await _open_channel_captcha(message, bot, payload[len(JOIN_PAYLOAD_PREFIX):])
        return

    if message.chat.type == ChatType.PRIVATE:
        if not logic.is_admin(message.from_user and message.from_user.id):
            await message.answer(f"Этот бот принадлежит {config.owner_contact}.")
            return
        text, kb = await dashboard.render_root()
        await message.answer(text, reply_markup=kb)
        return

    # /start прямо в группе/канале — сразу открываем карточку этого чата
    if not logic.is_admin(message.from_user and message.from_user.id):
        return
    result = await dashboard.render_chat(message.chat.id)
    if result is None:
        await message.answer("Ещё не видел ни одного события в этом чате — напиши /start ещё раз чуть позже.")
        return
    text, kb = result
    await message.answer(text, reply_markup=kb)


async def _open_channel_captcha(message: Message, bot: Bot, chat_id_str: str) -> None:
    try:
        target_chat_id = int(chat_id_str)
    except ValueError:
        await message.answer("Ссылка повреждена, попроси у администратора канала новую.")
        return

    settings = await logic.get_settings_snapshot(target_chat_id)
    if settings.join_blocked:
        await message.answer("Вход в этот канал сейчас полностью закрыт администратором.")
        return

    chat_title = settings.title
    if not chat_title:
        try:
            chat = await bot.get_chat(target_chat_id)
            chat_title = chat.title or ""
        except TelegramBadRequest:
            pass

    user = message.from_user

    if await cas.is_banned(user.id):
        await db.record_ban(target_chat_id, user.id, user.username or "", user.first_name or "", "CAS", "в базе CAS")
        await db.add_audit(target_chat_id, 0, "cas_ban", f"user={user.id}")
        await logic.notify_admins(
            bot, f"⛔️ {user.mention_html()} — попытка входа в «{chat_title}», но он в базе CAS.",
            chat_id=target_chat_id, chat_title=chat_title,
        )
        await message.answer("Не получилось выдать ссылку — обратись к администратору канала.")
        return

    if await db.is_trusted(user.id):
        try:
            link = await bot.create_chat_invite_link(target_chat_id, member_limit=1, name=f"trusted-{user.id}")
            await message.answer(f"Ты в доверенных — вот ссылка без капчи:\n{link.invite_link}")
        except TelegramBadRequest:
            await message.answer("У бота нет прав приглашать людей в этот канал.")
        return

    pm_chat_id = message.chat.id

    async def _on_success(bot: Bot, session: chat_captcha.CaptchaSession) -> None:
        try:
            link = await bot.create_chat_invite_link(target_chat_id, member_limit=1, name=f"verified-{user.id}")
        except TelegramBadRequest:
            await bot.send_message(pm_chat_id, "У бота нет прав приглашать людей в этот канал.")
            return
        if session.message_id:
            try:
                await bot.delete_message(pm_chat_id, session.message_id)
            except TelegramBadRequest:
                pass
        await bot.send_message(
            pm_chat_id, f"✅ Проверка пройдена! Держи персональную ссылку:\n{link.invite_link}"
        )

    async def _on_timeout(bot: Bot, session: chat_captcha.CaptchaSession) -> None:
        if session.message_id:
            try:
                await bot.delete_message(pm_chat_id, session.message_id)
            except TelegramBadRequest:
                pass
        await bot.send_message(pm_chat_id, "⌛ Время на проверку вышло. Напиши /start ещё раз, чтобы попробовать снова.")

    session = chat_captcha.create_session(
        bot, user.id, pm_chat_id, flow="channel_link", target_chat_id=target_chat_id,
        on_success=_on_success, on_timeout=_on_timeout,
    )
    caption = (
        f"Проверка для канала «{chat_title or target_chat_id}».\n\n" + chat_captcha.caption_text(user.mention_html())
    )
    msg = await media.send_animation_with_fallback(
        bot, pm_chat_id, media.CHAT_VIDEO,
        captions=[caption],
        keyboards=[chat_captcha.build_keyboard(session)],
    )
    if msg:
        session.message_id = msg.message_id
