from __future__ import annotations

import logging
from pathlib import Path

from aiogram import Bot
from aiogram.exceptions import TelegramBadRequest
from aiogram.types import FSInputFile, InlineKeyboardMarkup

logger = logging.getLogger(__name__)

ASSETS_DIR = Path(__file__).parent / "assets"
CHAT_VIDEO = ASSETS_DIR / "chat.mp4"   # "Проверка для чата ... готова, жми кнопку"
PREW_VIDEO = ASSETS_DIR / "prew.mp4"   # "Проверка пройдена, добро пожаловать"

# После первой успешной загрузки Telegram отдаёт file_id — дальше переиспользуем его,
# чтобы не заливать файл заново на каждое сообщение. Живёт в памяти процесса
# (после рестарта загрузится по новой один раз — это нормально).
_file_id_cache: dict[Path, str] = {}


def _source(path: Path):
    cached = _file_id_cache.get(path)
    return cached if cached else FSInputFile(path)


async def send_animation_with_fallback(
    bot: Bot,
    chat_id: int,
    video_path: Path,
    captions: list[str],
    keyboards: list[InlineKeyboardMarkup | None] | None = None,
):
    """Пытается отправить видео (как гифку/анимацию) с подписью и клавиатурой.
    captions — варианты текста от лучшего к простейшему (напр. премиум-эмодзи -> обычный).
    keyboards — варианты клавиатуры от лучшего к простейшему (напр. с иконкой -> без).
    Перебирает все комбинации; если видео не отправилось вообще ни разу — шлёт
    обычным текстом с той же логикой фолбэка по клавиатурам.
    Возвращает отправленное Message (или None, если вообще ничего не отправилось)."""
    kb_options: list[InlineKeyboardMarkup | None] = keyboards if keyboards else [None]

    for caption in captions:
        for kb in kb_options:
            try:
                msg = await bot.send_animation(chat_id, _source(video_path), caption=caption, reply_markup=kb)
                if msg.animation and video_path not in _file_id_cache:
                    _file_id_cache[video_path] = msg.animation.file_id
                return msg
            except TelegramBadRequest:
                continue

    logger.warning("Не удалось отправить видео %s ни в одном варианте, шлю текстом", video_path)
    for caption in captions:
        for kb in kb_options:
            try:
                return await bot.send_message(chat_id, caption, reply_markup=kb)
            except TelegramBadRequest:
                continue
    return None
