"""
Капча прямо в чате — без веб-версии (Mini App убран, см. MERGE_NOTES.md).

Механика: пользователю нужно собрать фразу "Я принимаю правила чата" из 4 кнопок,
по одной на каждое слово. У каждой позиции — 4 варианта слова (правильное + 3
отвлекающих), и клик по кнопке листает вариант на этой позиции по кругу. Как только
на всех 4 позициях оказывается правильное слово — проверка пройдена. На всё даётся
config.captcha_timeout_seconds (по умолчанию 90 сек), иначе — таймаут.

Модуль не знает, что конкретно делать при успехе/провале (снять ограничение в
группе, выдать ссылку на канал, одобрить заявку...) — это передаётся снаружи
через колбэки on_success/on_timeout при создании сессии (см. handlers/joins.py,
handlers/start.py, handlers/requests.py).
"""
from __future__ import annotations

import asyncio
import logging
import random
import secrets
import time
from dataclasses import dataclass, field
from typing import Awaitable, Callable

from aiogram import Bot, F, Router
from aiogram.exceptions import TelegramBadRequest
from aiogram.types import CallbackQuery, InlineKeyboardButton, InlineKeyboardMarkup

from config import config

logger = logging.getLogger("chat_captcha")
router = Router(name="chat_captcha")

TARGET_PHRASE = ["Я", "принимаю", "правила", "чата"]

# По 3 отвлекающих слова на каждую позицию (правильное добавляется отдельно и
# перемешивается вместе с ними)
_DISTRACTORS = [
    ["Он", "Мы", "Ты"],
    ["нарушаю", "читаю", "меняю"],
    ["музыку", "ссылку", "чаты"],
    ["группы", "канала", "бота"],
]

Callback = Callable[[Bot, "CaptchaSession"], Awaitable[None]]


@dataclass
class CaptchaSession:
    token: str
    user_id: int
    chat_id: int            # чат, где показана капча (группа либо ЛС с ботом)
    flow: str                # "group" | "channel_link" | "channel_request" — только для логов/отладки
    target_chat_id: int      # к какому чату/каналу относится проверка
    words: list[list[str]]   # 4 позиции × 4 варианта слова (перемешаны)
    indices: list[int]       # текущий выбранный вариант на каждой позиции
    message_id: int | None = None
    expires_at: float = 0.0
    task: asyncio.Task | None = None
    on_success: Callback | None = None
    on_timeout: Callback | None = None
    extra: dict = field(default_factory=dict)


sessions: dict[str, CaptchaSession] = {}


def _build_words() -> list[list[str]]:
    words = []
    for correct, distractors in zip(TARGET_PHRASE, _DISTRACTORS):
        options = [correct, *distractors]
        random.shuffle(options)
        words.append(options)
    return words


def create_session(
    bot: Bot,
    user_id: int,
    chat_id: int,
    flow: str,
    target_chat_id: int,
    on_success: Callback | None = None,
    on_timeout: Callback | None = None,
) -> CaptchaSession:
    words = _build_words()
    # стартуем не обязательно с правильного слова на позиции
    indices = [random.randrange(len(opts)) for opts in words]
    session = CaptchaSession(
        token=secrets.token_urlsafe(8),
        user_id=user_id,
        chat_id=chat_id,
        flow=flow,
        target_chat_id=target_chat_id,
        words=words,
        indices=indices,
        expires_at=time.time() + config.captcha_timeout_seconds,
        on_success=on_success,
        on_timeout=on_timeout,
    )
    sessions[session.token] = session
    session.task = asyncio.create_task(_timeout_watcher(bot, session))
    return session


async def _timeout_watcher(bot: Bot, session: CaptchaSession) -> None:
    try:
        await asyncio.sleep(config.captcha_timeout_seconds)
    except asyncio.CancelledError:
        return
    if sessions.pop(session.token, None) is None:
        return  # уже решена или удалена раньше
    if session.on_timeout:
        try:
            await session.on_timeout(bot, session)
        except Exception:
            logger.exception("Ошибка в on_timeout капчи (token=%s)", session.token)


def is_solved(session: CaptchaSession) -> bool:
    return all(session.words[pos][session.indices[pos]] == TARGET_PHRASE[pos] for pos in range(len(TARGET_PHRASE)))


def build_keyboard(session: CaptchaSession) -> InlineKeyboardMarkup:
    row = [
        InlineKeyboardButton(
            text=session.words[pos][session.indices[pos]],
            callback_data=f"ccap:{session.token}:{pos}",
        )
        for pos in range(len(session.words))
    ]
    return InlineKeyboardMarkup(inline_keyboard=[row])


def caption_text(mention: str) -> str:
    return (
        f"{mention}\n\n"
        f"CAPCHA:\n"
        f"Составь фразу:\n"
        f"«{' '.join(TARGET_PHRASE)}»\n\n"
        f"Жми на кнопки ниже, каждая листает своё слово — собери фразу целиком.\n"
        f"⏳ {config.captcha_timeout_seconds} сек, иначе — кик (можно зайти заново)."
    )


def cleanup_expired() -> None:
    now = time.time()
    for token in [t for t, s in sessions.items() if s.expires_at < now]:
        sessions.pop(token, None)


@router.callback_query(F.data.startswith("ccap:"))
async def on_captcha_button(callback: CallbackQuery, bot: Bot) -> None:
    try:
        _, token, pos_str = callback.data.split(":", 2)
        pos = int(pos_str)
    except (ValueError, AttributeError):
        await callback.answer()
        return

    session = sessions.get(token)
    if session is None or session.expires_at < time.time():
        sessions.pop(token, None)
        await callback.answer("⌛ Проверка устарела, попробуй зайти в чат заново.", show_alert=True)
        return

    if callback.from_user.id != session.user_id:
        await callback.answer("Это не твоя проверка 🙂", show_alert=True)
        return

    session.indices[pos] = (session.indices[pos] + 1) % len(session.words[pos])

    if is_solved(session):
        sessions.pop(token, None)
        if session.task:
            session.task.cancel()
        await callback.answer("✅ Готово!")
        if session.on_success:
            try:
                await session.on_success(bot, session)
            except Exception:
                logger.exception("Ошибка в on_success капчи (token=%s)", token)
        return

    try:
        await callback.message.edit_reply_markup(reply_markup=build_keyboard(session))
    except TelegramBadRequest:
        pass
    await callback.answer()
