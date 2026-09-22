"""
Сервис проверки подписок на каналы/группы — раньше отдельный FastAPI-сервис
(checker-main), теперь встроен в этого бота как модуль:
  - router      — aiogram Router (/set, callback "set:...", событие chat_member)
  - routes      — aiohttp-маршруты (/health, /stats, /stats/bots, /check, /check_many),
                   монтируются в тот же aiohttp-сервер, что и вебхук (см. server.py)
  - workers     — фоновые задачи, запускать через asyncio.create_task при старте (см. bot.py)

Логи по подпискам идут в темы (topics.py):
  - у каждого обращающегося клиент-бота (bot_name) — своя тема, создаётся при
    первом обращении этого бота ("регистрация"); ВСЕ логи checker'а по этому
    боту (в т.ч. результаты каждой проверки подписки) падают именно туда.
  - тема канала/группы (см. logic.notify_admins) — это отдельная штука для
    нативных событий anti-raid части (вход/выход участников в группах, которыми
    управляет сам этот бот), к checker'у она отношения не имеет.
"""
from __future__ import annotations

import hmac
import re
import time
import asyncio
import logging
from datetime import datetime, timezone, timedelta
from collections import Counter

from aiogram import Bot, Router, F
from aiogram.exceptions import TelegramRetryAfter, TelegramAPIError
from aiogram.filters import Command
from aiogram.types import (
    InlineKeyboardMarkup, InlineKeyboardButton, CallbackQuery, Message, ChatMemberUpdated,
)
from aiohttp import web

import db
import topics
from config import config

try:
    import redis.asyncio as aioredis
except ImportError:  # redis — необязательная зависимость, без неё всё работает в памяти
    aioredis = None

logger = logging.getLogger("checker")

router = Router(name="checker")
routes = web.RouteTableDef()

SUBSCRIBED_STATUSES = {"member", "administrator", "creator"}
MAX_RETRIES = 2
CHANNEL_ID_CACHE_TTL = 60 * 60 * 24 * 30
USERNAME_RE = re.compile(r'^[A-Za-z0-9_]{3,32}$')
CLEANUP_INTERVAL = 3600

redis_client = aioredis.from_url(config.checker_redis_url, decode_responses=True) if (aioredis and config.checker_redis_url) else None

# Общий семафор на вызовы Telegram Bot API, чтобы не словить каскад 429
_api_semaphore = asyncio.Semaphore(config.checker_api_concurrency)

_bot: Bot | None = None

# bot_name-ключи, для которых уже отправили приветственное "зарегистрирован" —
# защита от дублей при параллельных первых запросах одного и того же bot_name.
_registered_notified: set[str] = set()


def init(bot: Bot) -> None:
    """Вызывается один раз при старте (bot.py), чтобы модуль знал, каким Bot слать логи."""
    global _bot
    _bot = bot


def normalize_channel(channel: str) -> str:
    ch = channel.strip()
    if not ch:
        raise ValueError("Канал не может быть пустым")
    if ch.startswith("@"):
        ch = ch[1:].strip()
        if not USERNAME_RE.match(ch):
            raise ValueError(f"Некорректный username канала: {channel!r}")
        return "@" + ch.lower()
    if ch.lstrip("-").isdigit():
        return ch
    if not USERNAME_RE.match(ch):
        raise ValueError(f"Некорректный канал: {channel!r}")
    return "@" + ch.lower()


# ---------- Живое членство: заполняется из событий chat_member ----------
_membership: dict[tuple[str, int], bool] = {}
_membership_lock = asyncio.Lock()


async def get_membership(channel: str, user_id: int):
    if redis_client:
        val = await redis_client.get(f"membership:{channel}:{user_id}")
        return None if val is None else val == "1"
    async with _membership_lock:
        return _membership.get((channel, user_id))


async def set_membership(channel: str, user_id: int, subscribed: bool) -> None:
    if redis_client:
        await redis_client.set(f"membership:{channel}:{user_id}", "1" if subscribed else "0")
        return
    async with _membership_lock:
        _membership[(channel, user_id)] = subscribed


# ---------- Маппинг @username канала -> числовой chat_id ----------
_channel_id_cache: dict[str, int] = {}
_channel_id_lock = asyncio.Lock()


async def get_channel_id_cache(channel: str):
    if redis_client:
        val = await redis_client.get(f"chanid:{channel}")
        return int(val) if val is not None else None
    async with _channel_id_lock:
        return _channel_id_cache.get(channel)


async def set_channel_id_cache(channel: str, chat_id: int) -> None:
    if redis_client:
        await redis_client.set(f"chanid:{channel}", str(chat_id), ex=CHANNEL_ID_CACHE_TTL)
        return
    async with _channel_id_lock:
        _channel_id_cache[channel] = chat_id


async def resolve_channel_key(channel: str) -> str:
    if channel.lstrip("-").isdigit():
        return channel
    cached = await get_channel_id_cache(channel)
    if cached is not None:
        return str(cached)
    try:
        async with _api_semaphore:
            chat = await _bot.get_chat(channel)
        await set_channel_id_cache(channel, chat.id)
        return str(chat.id)
    except Exception as e:
        logger.warning(f"Не удалось резолвнуть канал {channel} в chat_id: {e}. Использую username как ключ.")
        return channel


# ---------- Ручные отметки админа ----------
_manual_override: dict[tuple[int, str], tuple[float, bool]] = {}
_manual_lock = asyncio.Lock()


async def get_manual_override(user_id: int, channel: str):
    if redis_client:
        val = await redis_client.get(f"manual:{channel}:{user_id}")
        return None if val is None else val == "1"
    async with _manual_lock:
        item = _manual_override.get((user_id, channel))
        if item is None:
            return None
        ts, subscribed = item
        if time.time() - ts > config.checker_manual_override_ttl:
            del _manual_override[(user_id, channel)]
            return None
        return subscribed


async def set_manual_override(user_id: int, channel: str, subscribed: bool) -> None:
    if redis_client:
        await redis_client.set(f"manual:{channel}:{user_id}", "1" if subscribed else "0", ex=config.checker_manual_override_ttl)
        return
    async with _manual_lock:
        _manual_override[(user_id, channel)] = (time.time(), subscribed)


# ---------- Кэш результатов (только успешные проверки) ----------
_cache: dict[tuple[int, str], tuple[float, bool]] = {}
_cache_lock = asyncio.Lock()

_fail_state: dict[tuple[int, str], dict] = {}
_fail_tasks: dict[tuple[int, str], asyncio.Task] = {}
_fail_lock = asyncio.Lock()

_inflight_calls: dict[tuple[int, str], asyncio.Future] = {}
_inflight_lock = asyncio.Lock()

_log_queue: asyncio.Queue = asyncio.Queue()

_error_buffer: Counter = Counter()
_error_lock = asyncio.Lock()

_last_notified: dict[tuple[int, str], float] = {}
_notify_lock = asyncio.Lock()


def format_user(user_id: int, username) -> str:
    return f"@{username} ({user_id})" if username else f"id {user_id}"


def build_override_keyboard(user_id: int, channel: str) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[[
        InlineKeyboardButton(text="✅ Подписан", callback_data=f"set:1:{user_id}:{channel}"),
        InlineKeyboardButton(text="❌ Не подписан", callback_data=f"set:0:{user_id}:{channel}"),
    ]])


async def should_notify(user_id: int, bot_name: str) -> bool:
    async with _notify_lock:
        key = (user_id, bot_name)
        last = _last_notified.get(key)
        now = time.time()
        if last is not None and now - last < config.checker_notify_dedupe_seconds:
            return False
        _last_notified[key] = now
        return True


async def record_error(reason: str) -> None:
    async with _error_lock:
        _error_buffer[reason] += 1


async def _resolve_thread(bot_name: str) -> int | None:
    """Тема для этого лога — тема клиент-бота (bot_name), который делает проверку.
    Тема канала — отдельная штука для нативных событий anti-raid части
    (см. logic.notify_admins), к логам checker'а отношения не имеет."""
    if config.topics_owner_id is None or _bot is None:
        return None
    return await topics.get_bot_topic(_bot, config.topics_owner_id, bot_name)


async def queue_log(text: str, markup: InlineKeyboardMarkup = None, *, bot_name: str = "?") -> None:
    await _log_queue.put((text, markup, bot_name))


async def ensure_bot_registered(bot_name: str) -> None:
    """При ПЕРВОМ обращении конкретного bot_name к checker'у ("регистрация") —
    создаёт для него тему в личке владельца и один раз пишет туда об этом.
    Дальше все логи checker'а по этому боту (включая результаты проверок
    подписки) идут в ту же тему — см. _resolve_thread."""
    if config.topics_owner_id is None or _bot is None:
        return
    key = topics._norm_bot_key(bot_name)
    if key in _registered_notified:
        return
    already = await db.get_topic_thread("bot", key)
    thread_id = await topics.get_bot_topic(_bot, config.topics_owner_id, bot_name)
    if already is None and thread_id is not None and key not in _registered_notified:
        _registered_notified.add(key)  # ставим ДО отправки — если два запроса пришли
        # одновременно, второй тоже мог пройти проверку `already is None` до того, как
        # первый успел создать тему; этот флаг не даёт продублировать приветствие.
        try:
            await _bot.send_message(
                config.topics_owner_id,
                f"🤖 Бот <b>{bot_name}</b> зарегистрирован и начал присылать логи.",
                message_thread_id=thread_id,
            )
        except Exception:
            pass


async def log_sender_worker():
    """Отправляет сообщения из очереди не чаще ~1/сек, чтобы не словить flood control."""
    while True:
        text, markup, bot_name = await _log_queue.get()
        try:
            if config.topics_owner_id is not None:
                thread_id = await _resolve_thread(bot_name)
                await _bot.send_message(config.topics_owner_id, text, reply_markup=markup, message_thread_id=thread_id)
            for admin_id in config.admin_ids:
                if admin_id == config.topics_owner_id:
                    continue
                try:
                    await _bot.send_message(admin_id, text, reply_markup=markup)
                except Exception:
                    pass
        except Exception as e:
            logger.warning(f"Не удалось отправить лог checker'а админу: {e}")
        await asyncio.sleep(1)


async def error_summary_worker():
    while True:
        await asyncio.sleep(config.checker_error_summary_interval)
        async with _error_lock:
            if not _error_buffer:
                continue
            lines = [f"⚠️ Сводка ошибок checker'а за последние {config.checker_error_summary_interval // 60} мин:"]
            for reason, count in _error_buffer.most_common():
                lines.append(f"• {reason} — {count} раз")
            _error_buffer.clear()
        await queue_log("\n".join(lines), bot_name="checker")


async def cleanup_worker():
    while True:
        await asyncio.sleep(CLEANUP_INTERVAL)
        if redis_client:
            continue
        now = time.time()
        cutoff_date = (datetime.now(timezone.utc) - timedelta(days=2)).strftime("%Y-%m-%d")

        async with _notify_lock:
            stale = [k for k, t in _last_notified.items() if now - t > config.checker_notify_dedupe_seconds * 10]
            for k in stale:
                del _last_notified[k]

        async with _users_lock:
            for d in [d for d in _daily_users if d < cutoff_date]:
                del _daily_users[d]

        async with _bot_visits_lock:
            for d in [d for d in _bot_visits_daily if d < cutoff_date]:
                del _bot_visits_daily[d]

        logger.info("Периодическая очистка checker'а выполнена")


@router.message(Command("set"))
async def cmd_set(message: Message):
    if message.from_user.id != config.topics_owner_id:
        return
    parts = message.text.split()
    if len(parts) != 4:
        await message.answer("Формат: /set <user_id> <channel> <true|false>\nНапример: /set 6283195301 @infoaboutqq true")
        return
    _, raw_user_id, raw_channel, raw_value = parts
    try:
        channel = normalize_channel(raw_channel)
    except ValueError as e:
        await message.answer(f"Некорректный канал: {e}")
        return
    try:
        user_id = int(raw_user_id)
    except ValueError:
        await message.answer("user_id должен быть числом")
        return
    value = raw_value.strip().lower()
    if value not in ("true", "false"):
        await message.answer("Последний аргумент должен быть true или false")
        return
    subscribed = value == "true"
    chan_key = await resolve_channel_key(channel)
    await set_manual_override(user_id, chan_key, subscribed)
    await message.answer(
        f"Записано ✏️: {format_user(user_id, None)}, канал {channel} — "
        f"{'подписан' if subscribed else 'не подписан'} "
        f"(держится {config.checker_manual_override_ttl // 60} мин, потом снова настоящая проверка)"
    )


@router.callback_query(F.data.startswith("set:"))
async def callback_set(callback: CallbackQuery):
    if callback.from_user.id != config.topics_owner_id:
        await callback.answer("Только для админа", show_alert=True)
        return
    _, value, raw_user_id, raw_channel = callback.data.split(":", 3)
    try:
        channel = normalize_channel(raw_channel)
    except ValueError:
        await callback.answer("Некорректный канал", show_alert=True)
        return
    user_id = int(raw_user_id)
    subscribed = value == "1"
    chan_key = await resolve_channel_key(channel)
    await set_manual_override(user_id, chan_key, subscribed)
    await callback.answer("Записано ✅" if subscribed else "Записано ❌")
    try:
        new_text = (
            callback.message.html_text
            + f"\n\n✏️ Вручную отмечено: {'подписан' if subscribed else 'не подписан'} "
            f"({channel}, держится {config.checker_manual_override_ttl // 60} мин)"
        )
        await callback.message.edit_text(new_text, reply_markup=None)
    except Exception:
        pass


async def update_membership_from_event(event: ChatMemberUpdated) -> None:
    """Обновляет кэш живого членства checker'а из любого chat_member-события.
    Вызывается явно из handlers/joins.py (on_member_join/on_member_leave) — так
    надёжнее, чем отдельный @router.chat_member() без фильтра: такой хендлер
    перехватывал бы ЛЮБОЕ событие первым и не давал бы отработать anti-raid части
    (в aiogram апдейт по умолчанию обрабатывает только первый подошедший хендлер)."""
    chat = event.chat
    chan_key = str(chat.id)
    user_id = event.new_chat_member.user.id
    subscribed = event.new_chat_member.status in SUBSCRIBED_STATUSES
    await set_membership(chan_key, user_id, subscribed)
    await set_cache(user_id, chan_key, subscribed)
    if chat.username:
        await set_channel_id_cache(normalize_channel(chat.username), chat.id)


@router.chat_member()
async def on_chat_member_update(event: ChatMemberUpdated):
    """Ловит те chat_member-события, которые НЕ являются вступлением/выходом
    (например, повышение/понижение прав) — join/leave уже обработаны и обновлены
    через update_membership_from_event() из joins.py до того, как событие сюда
    дойдёт. Этот роутер должен быть подключён В bot.py ПОСЛЕ роутера joins, иначе
    он снова перехватит все события первым."""
    await update_membership_from_event(event)


async def get_from_cache(user_id: int, channel: str):
    if redis_client:
        val = await redis_client.get(f"sub:{channel}:{user_id}")
        return None if val is None else val == "1"
    async with _cache_lock:
        item = _cache.get((user_id, channel))
        if item is None:
            return None
        ts, subscribed = item
        ttl = config.checker_subscribed_cache_ttl if subscribed else config.checker_not_subscribed_cache_ttl
        if time.time() - ts > ttl:
            del _cache[(user_id, channel)]
            return None
        return subscribed


async def set_cache(user_id: int, channel: str, subscribed: bool) -> None:
    if redis_client:
        ttl = config.checker_subscribed_cache_ttl if subscribed else config.checker_not_subscribed_cache_ttl
        await redis_client.set(f"sub:{channel}:{user_id}", "1" if subscribed else "0", ex=ttl)
        return
    async with _cache_lock:
        _cache[(user_id, channel)] = (time.time(), subscribed)


async def recovery_loop(user_id: int, channel: str, chan_key: str, bot_name: str, username):
    key = (user_id, chan_key)
    gave_up_notified = False
    fully_gave_up_notified = False

    while True:
        async with _fail_lock:
            state = _fail_state.get(key)
            if state is None:
                return
            attempts = state["attempts"]

        if attempts < config.checker_fail_max_fast_attempts:
            interval = config.checker_fail_retry_interval
        elif attempts < config.checker_fail_max_total_attempts:
            interval = config.checker_fail_slow_retry_interval
        else:
            interval = config.checker_fail_give_up_retry_interval

        await asyncio.sleep(interval)

        try:
            async with _api_semaphore:
                member = await _bot.get_chat_member(chat_id=chan_key, user_id=user_id)
            subscribed = member.status in SUBSCRIBED_STATUSES
            await set_cache(user_id, chan_key, subscribed)
            await set_membership(chan_key, user_id, subscribed)
            async with _fail_lock:
                _fail_state.pop(key, None)
                _fail_tasks.pop(key, None)
            await queue_log(
                f"✅ Восстановлено после ошибки\n"
                f"Бот <b>{bot_name}</b>, пользователь: {format_user(user_id, username)}\n"
                f"Канал {channel}: {'подписан' if subscribed else 'не подписан'}",
                bot_name=bot_name,
            )
            return

        except TelegramRetryAfter as e:
            await asyncio.sleep(e.retry_after)
            continue

        except Exception as e:
            async with _fail_lock:
                state = _fail_state.get(key)
                if state is None:
                    return
                state["attempts"] += 1
                attempts = state["attempts"]

            if attempts == config.checker_fail_max_fast_attempts and not gave_up_notified:
                gave_up_notified = True
                await record_error(
                    f"Не удалось восстановить за {attempts} попыток — {format_user(user_id, username)}, "
                    f"канал {channel}: {e.__class__.__name__}: {str(e)[:150]}. "
                    f"Дальше отказываю в доступе и пробую раз в {config.checker_fail_slow_retry_interval // 60} мин."
                )
            elif attempts == config.checker_fail_max_total_attempts and not fully_gave_up_notified:
                fully_gave_up_notified = True
                await record_error(
                    f"Сдался после {attempts} попыток — {format_user(user_id, username)}, канал {channel}. "
                    f"Доступ остаётся закрыт; дальше пробую тихо раз в "
                    f"{config.checker_fail_give_up_retry_interval // 3600} ч."
                )
            elif attempts < config.checker_fail_max_total_attempts:
                await record_error(
                    f"Всё ещё не восстановлено ({channel}, {format_user(user_id, username)}): "
                    f"{e.__class__.__name__}: {str(e)[:150]}"
                )
            continue


async def check_subscription(user_id: int, channel: str, bot_name: str, username):
    """Возвращает (subscribed, is_fail_open, reason)."""
    channel = normalize_channel(channel)
    chan_key = await resolve_channel_key(channel)

    manual = await get_manual_override(user_id, chan_key)
    if manual is not None:
        return manual, False, "manual"

    membership = await get_membership(chan_key, user_id)
    if membership is not None:
        return membership, False, None

    cached = await get_from_cache(user_id, chan_key)
    if cached is not None:
        return cached, False, None

    key = (user_id, chan_key)

    async with _fail_lock:
        fail_state = _fail_state.get(key)

    if fail_state is not None:
        if fail_state["attempts"] >= config.checker_fail_max_fast_attempts:
            return False, True, "gave_up"
        elapsed = time.time() - fail_state["since"]
        if elapsed < config.checker_fail_open_deny_seconds:
            return False, True, "deny_initial"
        return True, True, "grace"

    async with _inflight_lock:
        fut = _inflight_calls.get(key)
        is_leader = fut is None
        if is_leader:
            fut = asyncio.get_running_loop().create_future()
            _inflight_calls[key] = fut

    if not is_leader:
        return await fut

    result = (False, True, "deny_initial")
    try:
        attempt = 0
        succeeded = False
        while True:
            try:
                async with _api_semaphore:
                    member = await _bot.get_chat_member(chat_id=chan_key, user_id=user_id)
                subscribed = member.status in SUBSCRIBED_STATUSES
                await set_cache(user_id, chan_key, subscribed)
                await set_membership(chan_key, user_id, subscribed)
                result = (subscribed, False, None)
                succeeded = True
                break
            except TelegramRetryAfter as e:
                attempt += 1
                if attempt > MAX_RETRIES:
                    await record_error(f"429 от Telegram, канал {channel} (после {MAX_RETRIES} повторов)")
                    break
                await asyncio.sleep(e.retry_after)
                continue
            except TelegramAPIError as e:
                await record_error(f"Ошибка Telegram API ({channel}): {e.__class__.__name__}: {str(e)[:150]}")
                break
            except Exception as e:
                await record_error(f"Неизвестная ошибка ({channel}): {e.__class__.__name__}: {str(e)[:150]}")
                break

        if not succeeded:
            async with _fail_lock:
                _fail_state[key] = {"since": time.time(), "attempts": 0}
                if key not in _fail_tasks:
                    _fail_tasks[key] = asyncio.create_task(
                        recovery_loop(user_id, channel, chan_key, bot_name, username)
                    )
            result = (False, True, "deny_initial")

    except Exception as e:
        await record_error(f"Неожиданная ошибка в check_subscription ({channel}): {e.__class__.__name__}: {str(e)[:150]}")

    finally:
        async with _inflight_lock:
            _inflight_calls.pop(key, None)
        if not fut.done():
            fut.set_result(result)

    return result


def verify_key(request: web.Request) -> None:
    x_api_key = request.headers.get("X-API-Key")
    if not config.checker_api_key or not x_api_key or not hmac.compare_digest(x_api_key, config.checker_api_key):
        raise web.HTTPForbidden(reason="Forbidden: invalid API key")


# ---------- Учёт уникальных юзеров/ботов для статистики ----------
_all_users: set[int] = set()
_daily_users: dict[str, set[int]] = {}
_users_lock = asyncio.Lock()

_all_bots: Counter = Counter()
_bot_visits_daily: dict[str, Counter] = {}
_bot_visits_lock = asyncio.Lock()


def _today_str() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%d")


async def record_user_seen(user_id: int) -> None:
    today = _today_str()
    if redis_client:
        await redis_client.sadd(f"users:{today}", user_id)
        await redis_client.sadd("users:all", user_id)
        return
    async with _users_lock:
        _all_users.add(user_id)
        _daily_users.setdefault(today, set()).add(user_id)


async def get_user_stats():
    today = _today_str()
    if redis_client:
        today_count = await redis_client.scard(f"users:{today}")
        total_count = await redis_client.scard("users:all")
        return today_count, total_count
    async with _users_lock:
        return len(_daily_users.get(today, set())), len(_all_users)


async def record_bot_visit(bot_name: str) -> None:
    today = _today_str()
    if redis_client:
        await redis_client.hincrby(f"botvisits:{today}", bot_name, 1)
        await redis_client.hincrby("botvisits:all", bot_name, 1)
        return
    async with _bot_visits_lock:
        _all_bots[bot_name] += 1
        _bot_visits_daily.setdefault(today, Counter())[bot_name] += 1


async def get_bot_stats() -> list[dict]:
    today = _today_str()
    if redis_client:
        today_data = await redis_client.hgetall(f"botvisits:{today}")
        all_data = await redis_client.hgetall("botvisits:all")
        names = set(today_data) | set(all_data)
        return [
            {"bot_name": n, "visits_today": int(today_data.get(n, 0)), "visits_total": int(all_data.get(n, 0))}
            for n in sorted(names)
        ]
    async with _bot_visits_lock:
        today_data = _bot_visits_daily.get(today, Counter())
        return [
            {"bot_name": n, "visits_today": today_data.get(n, 0), "visits_total": count}
            for n, count in sorted(_all_bots.items())
        ]


# ---------------------- HTTP API (aiohttp) ----------------------

@routes.get("/checker/health")
async def health(request: web.Request) -> web.Response:
    redis_ok = None
    if redis_client:
        try:
            await redis_client.ping()
            redis_ok = True
        except Exception:
            redis_ok = False
    return web.json_response({"status": "ok", "redis": redis_ok})


@routes.get("/checker/stats")
async def stats(request: web.Request) -> web.Response:
    verify_key(request)
    today_count, total_count = await get_user_stats()
    return web.json_response({"users_today": today_count, "users_total": total_count, "date": _today_str()})


@routes.get("/checker/stats/bots")
async def stats_bots(request: web.Request) -> web.Response:
    verify_key(request)
    bots = await get_bot_stats()
    return web.json_response({"bots": bots, "date": _today_str()})


@routes.get("/checker/check")
async def check_single(request: web.Request) -> web.Response:
    verify_key(request)
    q = request.query
    try:
        user_id = int(q["user_id"])
        channel = normalize_channel(q["channel"])
    except (KeyError, ValueError) as e:
        raise web.HTTPBadRequest(reason=str(e))
    bot_name = q.get("bot_name", "неизвестный бот")
    username = q.get("username")
    notify = q.get("notify", "true").lower() != "false"

    await record_user_seen(user_id)
    if notify:
        await record_bot_visit(bot_name)
        await ensure_bot_registered(bot_name)
    subscribed, is_fail_open, reason = await check_subscription(user_id, channel, bot_name, username)

    if notify and await should_notify(user_id, bot_name):
        status_text = {
            "deny_initial": "⏳ временно отказано (первые секунды после сбоя, идёт переповерка)",
            "grace": "✅ временный доступ (Telegram API не ответил, идёт переповерка)",
            "gave_up": "🚫 отказано (устойчивая ошибка, проверка не проходит уже долго — пробуем в фоне)",
            "manual": f"✏️ вручную отмечено: {'подписан' if subscribed else 'не подписан'}",
        }.get(reason, "✅ подписан" if subscribed else "❌ не подписан")

        await queue_log(
            f"📋 Бот <b>{bot_name}</b> — /start\n"
            f"Пользователь: {format_user(user_id, username)}\n"
            f"Канал {channel}: {status_text}",
            markup=build_override_keyboard(user_id, channel),
            bot_name=bot_name,
        )

    return web.json_response({
        "user_id": user_id, "channel": channel, "subscribed": subscribed, "fail_open": is_fail_open,
    })


@routes.get("/checker/check_many")
async def check_many(request: web.Request) -> web.Response:
    verify_key(request)
    q = request.query
    try:
        user_id = int(q["user_id"])
        channel_list = [normalize_channel(c) for c in q["channels"].split(",") if c.strip()]
    except (KeyError, ValueError) as e:
        raise web.HTTPBadRequest(reason=str(e))
    if not channel_list:
        raise web.HTTPBadRequest(reason="Не указано ни одного канала")

    bot_name = q.get("bot_name", "неизвестный бот")
    username = q.get("username")
    notify = q.get("notify", "true").lower() != "false"

    await record_user_seen(user_id)
    if notify:
        await record_bot_visit(bot_name)
        await ensure_bot_registered(bot_name)

    results = {}
    reasons = {}
    any_fail_open = False
    for ch in channel_list:
        subscribed, is_fail_open, reason = await check_subscription(user_id, ch, bot_name, username)
        results[ch] = subscribed
        reasons[ch] = reason
        any_fail_open = any_fail_open or is_fail_open

    all_subscribed = all(results.values()) if results else False

    if notify and await should_notify(user_id, bot_name):
        reason_labels = {
            "deny_initial": "⏳ (первые секунды после сбоя)",
            "grace": "✅ (временный доступ, идёт переповерка)",
            "gave_up": "🚫 (устойчивая ошибка, пробуем в фоне)",
            "manual": "✏️ (отмечено вручную)",
        }
        lines = [f"📋 Бот <b>{bot_name}</b> — /start", f"Пользователь: {format_user(user_id, username)}"]
        keyboard_rows = []
        for ch, ok in results.items():
            label = reason_labels.get(reasons[ch])
            lines.append(f"{ch}: {label}" if label else f"{ch}: {'✅' if ok else '❌'}")
            keyboard_rows.append([
                InlineKeyboardButton(text=f"✅ {ch}", callback_data=f"set:1:{user_id}:{ch}"),
                InlineKeyboardButton(text=f"❌ {ch}", callback_data=f"set:0:{user_id}:{ch}"),
            ])
        lines.append(f"Итог: {'✅ все подписки есть' if all_subscribed else '❌ не хватает подписки'}")
        await queue_log(
            "\n".join(lines), markup=InlineKeyboardMarkup(inline_keyboard=keyboard_rows),
            bot_name=bot_name,
        )

    return web.json_response({
        "user_id": user_id, "channels": results, "all_subscribed": all_subscribed, "fail_open": any_fail_open,
    })


# ---------- Обратная совместимость со старыми путями (без префикса /checker) ----------
# Раньше checker был отдельным сервисом на своём домене, и клиентские боты дёргали
# /health, /stats, /stats/bots, /check, /check_many напрямую. Теперь всё под /checker/*
# (чтобы не пересекаться с вебхуком и остальным этого бота), но чтобы существующие
# клиентские боты не сломались без правок на их стороне — старые пути тоже работают,
# просто отдают то же самое, что и новые.
routes.get("/health")(health)
routes.get("/stats")(stats)
routes.get("/stats/bots")(stats_bots)
routes.get("/check")(check_single)
routes.get("/check_many")(check_many)
