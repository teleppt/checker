import os
from dataclasses import dataclass, field

from dotenv import load_dotenv

load_dotenv()


def _int_env(name: str, default: int) -> int:
    val = os.getenv(name)
    return int(val) if val else default


@dataclass
class Config:
    bot_token: str = os.getenv("BOT_TOKEN", "")
    admin_ids: set[int] = field(
        default_factory=lambda: {
            int(x) for x in os.getenv("ADMIN_IDS", "").replace(" ", "").split(",") if x
        }
    )
    # Владелец, в чью личку с ботом падают темы (createForumTopic сейчас работает
    # только в приватном чате конкретного пользователя — темы разных людей не связаны).
    # По умолчанию — наименьший ID из ADMIN_IDS (детерминированно, без сюрпризов
    # при рестарте), но лучше явно задать TOPICS_OWNER_ID, если админов несколько.
    topics_owner_id: int | None = field(default=None)
    db_path: str = os.getenv("DB_PATH", "bot.db")

    # ---------- Checker (проверка подписок) — раньше отдельный сервис, теперь встроен ----------
    checker_api_key: str = os.getenv("CHECKER_API_KEY", "")  # ключ для внешних клиентов /check, /check_many
    checker_redis_url: str = os.getenv("CHECKER_REDIS_URL", "")  # опционально, иначе всё в памяти процесса
    checker_subscribed_cache_ttl: int = _int_env("CHECKER_SUBSCRIBED_CACHE_TTL", 300)
    checker_not_subscribed_cache_ttl: int = _int_env("CHECKER_NOT_SUBSCRIBED_CACHE_TTL", 15)
    checker_fail_open_deny_seconds: int = _int_env("CHECKER_FAIL_OPEN_DENY_SECONDS", 5)
    checker_fail_retry_interval: int = _int_env("CHECKER_FAIL_RETRY_INTERVAL", 5)
    checker_fail_max_fast_attempts: int = _int_env("CHECKER_FAIL_MAX_FAST_ATTEMPTS", 12)
    checker_fail_slow_retry_interval: int = _int_env("CHECKER_FAIL_SLOW_RETRY_INTERVAL", 300)
    checker_fail_max_total_attempts: int = _int_env("CHECKER_FAIL_MAX_TOTAL_ATTEMPTS", 20)
    checker_fail_give_up_retry_interval: int = _int_env("CHECKER_FAIL_GIVE_UP_RETRY_INTERVAL", 86400)
    checker_error_summary_interval: int = _int_env("CHECKER_ERROR_SUMMARY_INTERVAL", 600)
    checker_api_concurrency: int = _int_env("CHECKER_API_CONCURRENCY", 20)
    checker_manual_override_ttl: int = _int_env("CHECKER_MANUAL_OVERRIDE_TTL", 300)
    checker_notify_dedupe_seconds: int = _int_env("CHECKER_NOTIFY_DEDUPE_SECONDS", 60)

    # ---------- Интеграция game-main ("Вавилон") <-> rp-main (RP-бот) ----------
    # Адрес game-main (его webapp, например https://vavilon-production.up.railway.app)
    # и секрет, известный ТОЛЬКО checker'у и game-main (никогда не выдаётся
    # клиентским ботам вроде RP-бота). Им подписываются запросы checker -> game-main.
    economy_service_url: str = os.getenv("ECONOMY_SERVICE_URL", "")
    economy_bridge_secret: str = os.getenv("ECONOMY_BRIDGE_SECRET", "")

    # Ключи для интеграционных маршрутов /checker/integration/* — ОТДЕЛЬНЫЕ от
    # общего checker_api_key (которым проверяются подписки). Формат в .env:
    #   INTEGRATION_BOT_KEYS=qqRPBot:длинный_случайный_ключ_рп_бота
    # (через запятую можно перечислить несколько ботов: "bot1:key1,bot2:key2").
    # Так утечка обычного checker_api_key (он менее чувствительный, известен
    # многим ботам) не даёт доступа к движению денег.
    integration_bot_keys: dict[str, str] = field(
        default_factory=lambda: {
            name.strip(): key.strip()
            for name, _, key in (
                pair.partition(":") for pair in os.getenv("INTEGRATION_BOT_KEYS", "").split(",") if pair.strip()
            )
            if name.strip() and key.strip()
        }
    )

    integration_max_credit_amount: int = _int_env("INTEGRATION_MAX_CREDIT_AMOUNT", 2000)
    integration_max_transfer_amount: int = _int_env("INTEGRATION_MAX_TRANSFER_AMOUNT", 50000)

    raid_join_threshold: int = _int_env("RAID_JOIN_THRESHOLD", 5)
    raid_window_seconds: int = _int_env("RAID_WINDOW_SECONDS", 30)
    raid_captcha_duration: int = _int_env("RAID_CAPTCHA_DURATION", 900)

    # Экстренный уровень защиты поверх обычного антирейда: если за EXTREME_RAID_WINDOW_SECONDS
    # вступает/подаёт заявку EXTREME_RAID_THRESHOLD и более человек — это уже не "включить
    # капчу", а сразу банить всех, кто попал в этот всплеск (группы) или отклонять все их
    # заявки разом (каналы). Порог должен быть заметно выше обычного RAID_JOIN_THRESHOLD.
    extreme_raid_window_seconds: int = _int_env("EXTREME_RAID_WINDOW_SECONDS", 30)
    extreme_raid_threshold: int = _int_env("EXTREME_RAID_THRESHOLD", 10)

    # Через сколько секунд удалять нативные системные сообщения Telegram
    # ("Х присоединился к группе" / "Х вышел из группы")
    service_message_delete_seconds: int = _int_env("SERVICE_MESSAGE_DELETE_SECONDS", 20)

    # Если сообщение старше этого возраста (по умолчанию сутки) отредактировали —
    # удаляем его. Классический приём накрутки: написать безобидный текст, дождаться
    # доверия/охвата, потом отредактировать в рекламу/скам. Админов чата не трогаем.
    edited_message_age_threshold_seconds: int = _int_env("EDITED_MESSAGE_AGE_THRESHOLD_SECONDS", 86400)

    captcha_timeout_seconds: int = _int_env("CAPTCHA_TIMEOUT_SECONDS", 90)

    # Через сколько секунд авто-отклонять заявку на вступление, если капча так и не пройдена
    # (защита от того, чтобы заявки от накрутки просто копились без дела)
    request_decline_seconds: int = _int_env("REQUEST_DECLINE_SECONDS", 3600)

    # Публичный HTTPS-адрес, на котором крутится веб-версия капчи (Mini App).
    # На Railway это домен сервиса, например https://your-app.up.railway.app
    webapp_url: str = os.getenv("WEBAPP_URL", "").rstrip("/")

    # Порт, на котором поднимаем aiohttp-сервер с мини-аппом (Railway сам подставит $PORT)
    port: int = _int_env("PORT", 8080)

    # Бот закрытый — им управляет только владелец (первый/единственный ID в ADMIN_IDS).
    # Всем остальным в личке вместо меню показываем эту подпись.
    owner_contact: str = os.getenv("OWNER_CONTACT", "@infoaboutqq")

    # Проверка по базе известных спам-ботов CAS (Combot Anti-Spam System, cas.chat).
    # При вступлении сверяем ID, и если он там — баним сразу, до капчи.
    cas_enabled: bool = os.getenv("CAS_ENABLED", "true").lower() not in ("0", "false", "no")
    cas_api_url: str = os.getenv("CAS_API_URL", "https://api.cas.chat/check")
    cas_timeout_seconds: int = _int_env("CAS_TIMEOUT_SECONDS", 5)

    # Эвристики подозрительности: если капча выключена для чата, но новый участник похож
    # на бота (нет фото профиля + шаблонный юзернейм из букв и цифр), капчу всё равно покажем.
    heuristics_enabled: bool = os.getenv("HEURISTICS_ENABLED", "true").lower() not in ("0", "false", "no")

    # Антифлуд по сообщениям в группах
    flood_window_seconds: int = _int_env("FLOOD_WINDOW_SECONDS", 10)
    flood_message_threshold: int = _int_env("FLOOD_MESSAGE_THRESHOLD", 6)
    flood_repeat_threshold: int = _int_env("FLOOD_REPEAT_THRESHOLD", 3)
    flood_mute_seconds: int = _int_env("FLOOD_MUTE_SECONDS", 300)

    # Секрет для пути вебхука (часть URL, чтобы не принимать чужие POST-запросы).
    # Если пусто — сгенерируется случайный при старте (тогда после рестарта поменяется,
    # это не проблема, просто бот сам себе установит новый вебхук).
    webhook_secret: str = os.getenv("WEBHOOK_SECRET", "")

    # ID премиум-эмодзи для оформления (Bot API 9.4). Работают, только если у ВЛАДЕЛЬЦА
    # бота есть подписка Telegram Premium — иначе бот сам откатится на обычные эмодзи.
    emoji_wave_id: str = os.getenv("EMOJI_WAVE_ID", "5458904472598095631")   # 👋 в приветствии капчи
    emoji_check_id: str = os.getenv("EMOJI_CHECK_ID", "5429615935260470855")  # ✅ при успешной проверке
    emoji_ghost_id: str = os.getenv("EMOJI_GHOST_ID", "5305388752162539722")  # не используется (была иконка кнопки веб-капчи)


config = Config()

if config.topics_owner_id is None:
    config.topics_owner_id = min(config.admin_ids) if config.admin_ids else None
    _explicit_owner = os.getenv("TOPICS_OWNER_ID")
    if _explicit_owner:
        config.topics_owner_id = int(_explicit_owner)

if not config.bot_token:
    raise RuntimeError("BOT_TOKEN не задан. Заполни .env (см. .env.example)")

if not config.webapp_url:
    raise RuntimeError(
        "WEBAPP_URL не задан. Вебхуку бота нужен публичный HTTPS-адрес — "
        "включи публичный домен в Railway и укажи его в .env (см. .env.example)"
    )

if not config.webhook_secret:
    import secrets as _secrets
    config.webhook_secret = _secrets.token_urlsafe(24)
