from __future__ import annotations

import time
from collections import defaultdict

from aiogram import Bot, F, Router
from aiogram.enums import ChatType
from aiogram.exceptions import TelegramBadRequest
from aiogram.types import ChatPermissions, Message

import db
import logic
from config import config

router = Router(name="antiflood")

# (chat_id, user_id) -> [(timestamp, text), ...] за последнее окно
_recent: dict[tuple[int, int], list[tuple[float, str]]] = defaultdict(list)


@router.message(F.chat.type.in_({ChatType.GROUP, ChatType.SUPERGROUP}), F.text | F.caption)
async def check_flood(message: Message, bot: Bot) -> None:
    if message.from_user is None or message.from_user.is_bot:
        return

    chat_id = message.chat.id
    user_id = message.from_user.id

    if logic.is_admin(user_id) or await db.is_trusted(user_id):
        return

    settings = await logic.get_settings_snapshot(chat_id)
    if not settings.antiflood_enabled:
        return

    now = time.time()
    text = message.text or message.caption or ""
    key = (chat_id, user_id)
    bucket = _recent[key]
    bucket.append((now, text))

    cutoff = now - config.flood_window_seconds
    while bucket and bucket[0][0] < cutoff:
        bucket.pop(0)

    total = len(bucket)
    same_text_count = sum(1 for _, t in bucket if text and t == text)

    is_flood = total >= config.flood_message_threshold or (
        text and same_text_count >= config.flood_repeat_threshold
    )
    if not is_flood:
        return

    _recent.pop(key, None)

    try:
        await bot.restrict_chat_member(
            chat_id,
            user_id,
            permissions=ChatPermissions(can_send_messages=False),
            until_date=int(now) + config.flood_mute_seconds,
        )
    except TelegramBadRequest:
        pass

    try:
        await message.delete()
    except TelegramBadRequest:
        pass

    await logic.notify_admins(
        bot,
        f"🔇 {message.from_user.mention_html()} замьючен на {config.flood_mute_seconds // 60} мин "
        f"в «{logic.chat_display_name(settings)}» — похоже на флуд одинаковыми сообщениями.",
        chat_id=chat_id, chat_title=logic.chat_display_name(settings),
    )
    await db.add_audit(chat_id, 0, "antiflood_mute", f"user={user_id}")
