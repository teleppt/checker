# === Добавить внутрь класса Config в checker-main/config.py ===
#
# Вставить рядом с существующими checker_* полями. Ничего из уже
# существующего трогать не нужно.

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
