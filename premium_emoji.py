from __future__ import annotations

from typing import Any

from aiogram import Bot
from aiogram.exceptions import TelegramBadRequest
from aiogram.types import Message

from config import config

# Премиум-эмодзи требуют, чтобы у ВЛАДЕЛЬЦА бота была подписка Telegram Premium
# (Bot API 9.4). Если её нет — Telegram отклонит сообщение с ошибкой, поэтому
# везде ниже есть запасной вариант с обычным юникод-эмодзи.
WAVE = config.emoji_wave_id
CHECK = config.emoji_check_id
GHOST = config.emoji_ghost_id


def tag(emoji_id: str, fallback: str) -> str:
    return f'<tg-emoji emoji-id="{emoji_id}">{fallback}</tg-emoji>'


async def send_html(bot: Bot, chat_id: int, premium_text: str, plain_text: str, **kwargs: Any) -> Message | None:
    """Отправляет premium_text; если Telegram отклонил (нет Premium у владельца
    или битый emoji-id) — молча отправляет plain_text вместо него."""
    try:
        return await bot.send_message(chat_id, premium_text, **kwargs)
    except TelegramBadRequest:
        try:
            return await bot.send_message(chat_id, plain_text, **kwargs)
        except TelegramBadRequest:
            return None


async def edit_html(bot: Bot, chat_id: int, message_id: int, premium_text: str, plain_text: str, **kwargs: Any) -> None:
    try:
        await bot.edit_message_text(premium_text, chat_id=chat_id, message_id=message_id, **kwargs)
    except TelegramBadRequest:
        try:
            await bot.edit_message_text(plain_text, chat_id=chat_id, message_id=message_id, **kwargs)
        except TelegramBadRequest:
            pass


def icon_kwargs(emoji_id: str) -> dict:
    """kwargs для InlineKeyboardButton(**icon_kwargs(...)) — если поле не примет
    Telegram (нет Premium у владельца), кнопка просто останется без иконки."""
    return {"icon_custom_emoji_id": emoji_id}
