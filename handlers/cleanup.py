"""
Автоудаление нативных системных сообщений Telegram — тех самых "X присоединился к группе" /
"X покинул группу", которые Telegram сам вставляет в ленту чата (это обычные Message с
заполненным new_chat_members/left_chat_member, отдельная сущность от chat_member-апдейтов,
на которых работает вся капча/антирейд — их не трогаем).

Плюс защита от классического приёма накрутки: написать безобидное сообщение, дождаться
доверия/охвата, потом отредактировать его в рекламу/скам. Если правят сообщение старше
EDITED_MESSAGE_AGE_THRESHOLD_SECONDS — удаляем, кроме сообщений админов чата.

Требует у бота право "Удаление сообщений" в группе — без него просто тихо ничего не сделает.
"""
from __future__ import annotations

import asyncio

from aiogram import Bot, F, Router
from aiogram.enums import ChatType
from aiogram.exceptions import TelegramBadRequest
from aiogram.types import Message

from config import config

router = Router(name="cleanup")


async def _delete_after(bot: Bot, chat_id: int, message_id: int, delay: int) -> None:
    await asyncio.sleep(delay)
    try:
        await bot.delete_message(chat_id, message_id)
    except TelegramBadRequest:
        pass  # нет прав, сообщение уже удалено и т.п. — не критично


@router.message(F.new_chat_members | F.left_chat_member)
async def auto_delete_service_message(message: Message, bot: Bot) -> None:
    asyncio.create_task(
        _delete_after(bot, message.chat.id, message.message_id, config.service_message_delete_seconds)
    )


@router.edited_message(F.chat.type.in_({ChatType.GROUP, ChatType.SUPERGROUP}))
async def delete_old_edited_message(message: Message, bot: Bot) -> None:
    if message.from_user is None or message.from_user.is_bot:
        return
    if not message.edit_date:
        return

    age = message.edit_date - message.date.timestamp()
    if age < config.edited_message_age_threshold_seconds:
        return

    try:
        member = await bot.get_chat_member(message.chat.id, message.from_user.id)
    except TelegramBadRequest:
        return  # не смогли проверить статус — на всякий случай не трогаем
    if member.status in ("administrator", "creator"):
        return

    try:
        await bot.delete_message(message.chat.id, message.message_id)
    except TelegramBadRequest:
        pass
