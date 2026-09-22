from __future__ import annotations

import re

from aiogram import Bot
from aiogram.types import User

from config import config

# Юзернеймы вида "user18234756", "Anna029384756" — слово + 5+ цифр подряд, без разделителей
_TEMPLATE_USERNAME_RE = re.compile(r"^[A-Za-z]*\d{5,}$")


async def is_suspicious(bot: Bot, user: User) -> bool:
    """Похож на свежесозданного бота: шаблонный юзернейм И нет фото профиля.
    Оба признака сразу — чтобы не давить капчой обычных людей просто за отсутствие фото."""
    if not config.heuristics_enabled:
        return False

    username_suspicious = bool(user.username) and bool(_TEMPLATE_USERNAME_RE.match(user.username))
    if not username_suspicious:
        return False

    try:
        photos = await bot.get_user_profile_photos(user.id, limit=1)
        no_photo = photos.total_count == 0
    except Exception:
        return False  # не смогли проверить — не считаем подозрительным по этой причине

    return no_photo
