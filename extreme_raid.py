"""
Экстренный уровень защиты поверх обычного антирейда (см. joins.py/requests.py — там же
живёт "мягкий" детект, который просто включает капчу).

Тут — жёсткая реакция: если за EXTREME_RAID_WINDOW_SECONDS секунд (по умолчанию 30) в чат
ломится EXTREME_RAID_THRESHOLD и больше человек — не ждём, банит/отклоняет всех разом.

Список "кого банить" для групп берём из recent_joiners (state.py) — трекера в памяти,
а НЕ из базы данных. При настоящем массовом рейде (сотни вступлений за секунды) запись
в SQLite может не успевать под нагрузкой, и если брать список из БД, можно забанить
пустое множество просто потому, что записи ещё не долетели до диска.
"""
from __future__ import annotations

import asyncio
import logging
import time

from aiogram import Bot
from aiogram.exceptions import TelegramBadRequest

import db
import logic
from config import config
from state import decline_tasks, extreme_ban_in_progress, join_tracker, recent_joiners

logger = logging.getLogger(__name__)

# Сколько банов гонять параллельно. При сотнях целей строго последовательно (даже
# с маленькой паузой) — это минуты; с этим — десятки секунд, а Telegram спокойно
# держит десятки параллельных вызовов от одного бота.
BAN_CONCURRENCY = 20


def _triggered(chat_id: int) -> bool:
    if chat_id in extreme_ban_in_progress:
        return False
    count = join_tracker.count(chat_id, config.extreme_raid_window_seconds)
    return count >= config.extreme_raid_threshold


async def handle_group_burst(bot: Bot, chat_id: int) -> bool:
    if not _triggered(chat_id):
        return False
    extreme_ban_in_progress.add(chat_id)
    asyncio.create_task(_safe_run(_run_group_ban, bot, chat_id))
    return True


async def handle_channel_request_burst(bot: Bot, chat_id: int) -> bool:
    if not _triggered(chat_id):
        return False
    extreme_ban_in_progress.add(chat_id)
    asyncio.create_task(_safe_run(_run_channel_decline, bot, chat_id))
    return True


async def _safe_run(coro_func, bot: Bot, chat_id: int) -> None:
    """Обёртка, которая гарантированно снимает блокировку и не даёт упасть тихо —
    если что-то пошло не так, это будет видно в логах, а не просто "ничего не произошло"."""
    try:
        await coro_func(bot, chat_id)
    except Exception:
        logger.exception("extreme_raid: сбой при обработке чата %s", chat_id)
        try:
            await logic.notify_admins(
                bot, f"Экстренный авто-бан в чате {chat_id} упал с ошибкой — смотри логи Railway.",
                chat_id=chat_id,
            )
        except Exception:
            pass
    finally:
        extreme_ban_in_progress.discard(chat_id)


async def _run_group_ban(bot: Bot, chat_id: int) -> None:
    joiners = recent_joiners.since(chat_id, config.extreme_raid_window_seconds)
    if not joiners:
        return

    trusted_ids = await db.get_trusted_ids()
    targets = [j for j in joiners if j.user_id not in trusted_ids]
    if not targets:
        return

    settings = await logic.get_settings_snapshot(chat_id)
    name = logic.chat_display_name(settings)
    await logic.notify_admins(
        bot,
        f"Экстренно: за последние {config.extreme_raid_window_seconds} сек в «{name}» "
        f"вступило {len(targets)} чел. — баню всех разом, не дожидаясь капчи.",
        chat_id=chat_id, chat_title=name,
    )

    banned = 0
    failed = 0
    sem = asyncio.Semaphore(BAN_CONCURRENCY)

    async def _ban_one(j) -> None:
        nonlocal banned, failed
        async with sem:
            try:
                await bot.ban_chat_member(chat_id, j.user_id)
            except TelegramBadRequest:
                failed += 1
                return
            banned += 1
            recent_joiners.clear_user(chat_id, j.user_id)
            # запись в БД — best-effort, отдельно от самого бана: если база тормозит
            # или спотыкается под нагрузкой, человек всё равно уже забанен в Telegram
            try:
                await db.record_ban(
                    chat_id, j.user_id, j.username, j.first_name, "auto",
                    f"экстренный авто-бан: {len(targets)} вступлений за {config.extreme_raid_window_seconds} сек",
                )
                await db.record_member_leave(chat_id, j.user_id)
            except Exception:
                logger.exception("extreme_raid: не записал в БД бан user=%s chat=%s", j.user_id, chat_id)

    await asyncio.gather(*(_ban_one(j) for j in targets))

    try:
        await db.add_audit(chat_id, 0, "extreme_ban", f"banned={banned} failed={failed}")
    except Exception:
        logger.exception("extreme_raid: не записал аудит-лог chat=%s", chat_id)

    report = f"Готово: забанено {banned} чел. в «{name}»"
    report += f", не получилось у {failed}." if failed else "."
    await logic.notify_admins(bot, report, chat_id=chat_id, chat_title=name)


async def _run_channel_decline(bot: Bot, chat_id: int) -> None:
    keys = [key for key in list(decline_tasks.keys()) if key[0] == chat_id]
    if not keys:
        return

    settings = await logic.get_settings_snapshot(chat_id)
    name = logic.chat_display_name(settings)
    await logic.notify_admins(
        bot,
        f"Экстренно: за последние {config.extreme_raid_window_seconds} сек в «{name}» "
        f"пришло {len(keys)} заявок на вступление — отклоняю все разом.",
        chat_id=chat_id, chat_title=name,
    )

    declined = 0
    sem = asyncio.Semaphore(BAN_CONCURRENCY)

    async def _decline_one(key) -> None:
        nonlocal declined
        async with sem:
            task = decline_tasks.pop(key, None)
            if task:
                task.cancel()
            try:
                await bot.decline_chat_join_request(key[0], key[1])
                declined += 1
            except TelegramBadRequest:
                pass

    await asyncio.gather(*(_decline_one(key) for key in keys))

    try:
        await db.add_audit(chat_id, 0, "extreme_decline", f"declined={declined}")
    except Exception:
        logger.exception("extreme_raid: не записал аудит-лог chat=%s", chat_id)

    await logic.notify_admins(bot, f"Готово: отклонено {declined} заявок в «{name}».", chat_id=chat_id, chat_title=name)
