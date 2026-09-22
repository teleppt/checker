"""
Интеграционный мост между двумя ботами на разных серверах: RP-ботом (rp-main,
"qqRPBot") и экономическим ботом "Вавилон" (game-main, владелец баланса Qlon
и записей о браках). Оба бота уже умеют ходить в checker (см. client_example.py /
app/services/checker_client.py) — этот модуль добавляет поверх того же checker'а
ещё один набор маршрутов, которые НЕ проверяют подписки, а служат единственным
разрешённым каналом связи между двумя экономиками.

Почему через checker, а не напрямую bot <-> bot:
  - RP-бот никогда не получает секрет/ключ, которым можно напрямую дёргать
    "начисли/спиши Qlon" на game-main. У RP-бота есть только его собственный
    ключ к checker'у (как и раньше — X-API-Key + bot_name).
  - checker проверяет, что именно ЭТОТ bot_name (по отдельному, specific-to-
    integration ключу, см. INTEGRATION_BOT_KEYS) вообще имеет право дёргать
    интеграцию, и что запрашиваемое действие (reason) ему разрешено.
  - Дальше checker сам, отдельным секретом (ECONOMY_BRIDGE_SECRET), которого
    RP-бот не знает и никогда не увидит, подписывает запрос к game-main
    (HMAC-SHA256 по телу + timestamp, защита от replay) и пересылает его.
  - Если утечёт ключ RP-бота к checker'у — им можно только вызвать разрешённые
    интеграционные действия (начислить небольшую награду за игру, предложить
    брак, сделать перевод — от одного конкретного bot_name, всё это видно в
    логах), но НЕЛЬЗЯ ни зайти в чужой аккаунт, ни прочитать переписку/пароли
    (их тут просто нет в схеме данных), ни напрямую тронуть базу Вавилона.
  - Если утечёт ECONOMY_BRIDGE_SECRET — это отдельный секрет, известный только
    checker'у и game-main, и он никогда не передаётся клиентским ботам.

В payload'ах ЕСТЬ ТОЛЬКО: user_id (Telegram id), суммы Qlon, коды причин/статусов
и idempotency-ключи. НЕТ: текстов сообщений, истории переписки, паролей,
токенов ботов, initData и т.п. — так и задумано (см. ТЗ пользователя).
"""
from __future__ import annotations

import hashlib
import hmac
import json
import logging
import time
import uuid

import aiohttp
from aiohttp import web

import checker_core
from config import config

logger = logging.getLogger("integration")

routes = web.RouteTableDef()

_REQUEST_TIMEOUT = aiohttp.ClientTimeout(total=10)
_session: "aiohttp.ClientSession | None" = None

# Сколько секунд действителен подписанный запрос к game-main (защита от replay,
# если кто-то перехватит и повторит уже отправленный HTTP-запрос).
SIGNATURE_TTL = 60

# Разрешённые "причины" начисления Qlon и кто ими может пользоваться — держим
# белым списком, чтобы интеграция не превратилась в универсальный "напечатай
# сколько угодно денег кому угодно". Каждая пара (bot_name, reason) — это то,
# что реально разрешено вызывать снаружи.
ALLOWED_CREDIT_REASONS: dict[str, set[str]] = {
    "qqRPBot": {"rp_game_win", "rp_marriage_bonus"},
}

# Кто вообще имеет право дёргать интеграционные маршруты (помимо reason-листа
# выше — для transfer/marriage, где reason не нужен).
ALLOWED_INTEGRATION_BOTS: set[str] = set(config.integration_bot_keys.keys())

MAX_CREDIT_AMOUNT = config.integration_max_credit_amount
MAX_TRANSFER_AMOUNT = config.integration_max_transfer_amount


async def _get_session() -> aiohttp.ClientSession:
    global _session
    if _session is None or _session.closed:
        _session = aiohttp.ClientSession(timeout=_REQUEST_TIMEOUT)
    return _session


def _sign(body_bytes: bytes, timestamp: str) -> str:
    msg = timestamp.encode() + b"." + body_bytes
    return hmac.new(config.economy_bridge_secret.encode(), msg, hashlib.sha256).hexdigest()


async def _call_economy_service(path: str, payload: dict) -> tuple[int, dict]:
    """Подписывает и шлёт запрос на game-main (единственное место, которое знает
    ECONOMY_BRIDGE_SECRET на этой стороне). Возвращает (http_status, json_body)."""
    if not config.economy_service_url or not config.economy_bridge_secret:
        logger.error("ECONOMY_SERVICE_URL/ECONOMY_BRIDGE_SECRET не заданы — интеграция недоступна")
        return 503, {"ok": False, "error": "economy_service_not_configured"}

    body_bytes = json.dumps(payload, separators=(",", ":"), sort_keys=True).encode()
    timestamp = str(int(time.time()))
    signature = _sign(body_bytes, timestamp)

    url = f"{config.economy_service_url.rstrip('/')}{path}"
    try:
        session = await _get_session()
        async with session.post(
            url,
            data=body_bytes,
            headers={
                "Content-Type": "application/json",
                "X-Bridge-Timestamp": timestamp,
                "X-Bridge-Signature": signature,
            },
        ) as resp:
            try:
                data = await resp.json()
            except (aiohttp.ContentTypeError, json.JSONDecodeError):
                data = {"ok": False, "error": f"bad_response_status_{resp.status}"}
            return resp.status, data
    except (aiohttp.ClientError, TimeoutError) as e:
        logger.warning("game-main недоступен (%s: %s)", e.__class__.__name__, e)
        return 502, {"ok": False, "error": "economy_service_unreachable"}


def _verify_integration_key(request: web.Request) -> str:
    """Проверяет X-API-Key ОТДЕЛЬНО от обычного checker_api_key — интеграция
    (движение денег) намеренно требует своего, более узкого ключа на bot_name,
    а не общего ключа, которым дёргают /checker/check. Возвращает bot_name."""
    bot_name = request.headers.get("X-Bot-Name", "")
    api_key = request.headers.get("X-API-Key", "")
    expected = config.integration_bot_keys.get(bot_name)
    if not bot_name or not expected or not api_key or not hmac.compare_digest(api_key, expected):
        raise web.HTTPForbidden(reason="Forbidden: invalid integration credentials")
    return bot_name


async def _audit(bot_name: str, action: str, detail: str) -> None:
    await checker_core.queue_log(
        f"🔗 Интеграция · <b>{bot_name}</b> → {action}\n{detail}", bot_name=bot_name,
    )


@routes.post("/checker/integration/profile")
async def integration_profile(request: web.Request) -> web.Response:
    bot_name = _verify_integration_key(request)
    try:
        body = await request.json()
        user_id = int(body["user_id"])
    except (KeyError, ValueError, TypeError, json.JSONDecodeError):
        raise web.HTTPBadRequest(reason="bad_request_body")

    status, data = await _call_economy_service(
        "/integration/profile", {"user_id": user_id, "source_bot": bot_name},
    )
    return web.json_response(data, status=status if status else 200)


def _new_idempotency_key() -> str:
    return uuid.uuid4().hex


@routes.post("/checker/integration/game/credit")
async def integration_credit(request: web.Request) -> web.Response:
    bot_name = _verify_integration_key(request)
    try:
        body = await request.json()
        user_id = int(body["user_id"])
        amount = int(body["amount"])
        reason = str(body["reason"])
        idem = str(body.get("idempotency_key") or _new_idempotency_key())
    except (KeyError, ValueError, TypeError, json.JSONDecodeError):
        raise web.HTTPBadRequest(reason="bad_request_body")

    if amount <= 0 or amount > MAX_CREDIT_AMOUNT:
        raise web.HTTPBadRequest(reason="amount_out_of_range")
    if reason not in ALLOWED_CREDIT_REASONS.get(bot_name, set()):
        raise web.HTTPForbidden(reason="reason_not_allowed_for_bot")

    status, data = await _call_economy_service(
        "/integration/credit",
        {"user_id": user_id, "amount": amount, "reason": reason, "idempotency_key": idem,
         "source_bot": bot_name},
    )
    await _audit(bot_name, "credit", f"user_id={user_id} amount={amount} reason={reason} → {data}")
    return web.json_response(data, status=status if status else 200)


@routes.post("/checker/integration/game/transfer")
async def integration_transfer(request: web.Request) -> web.Response:
    bot_name = _verify_integration_key(request)
    try:
        body = await request.json()
        from_user_id = int(body["from_user_id"])
        to_user_id = int(body["to_user_id"])
        amount = int(body["amount"])
        idem = str(body.get("idempotency_key") or _new_idempotency_key())
    except (KeyError, ValueError, TypeError, json.JSONDecodeError):
        raise web.HTTPBadRequest(reason="bad_request_body")

    if amount <= 0 or amount > MAX_TRANSFER_AMOUNT:
        raise web.HTTPBadRequest(reason="amount_out_of_range")
    if from_user_id == to_user_id:
        raise web.HTTPBadRequest(reason="cannot_transfer_to_self")

    status, data = await _call_economy_service(
        "/integration/transfer",
        {"from_user_id": from_user_id, "to_user_id": to_user_id, "amount": amount,
         "idempotency_key": idem, "source_bot": bot_name},
    )
    await _audit(
        bot_name, "transfer",
        f"{from_user_id} → {to_user_id}, amount={amount} → {data}",
    )
    return web.json_response(data, status=status if status else 200)


@routes.post("/checker/integration/marriage/sync")
async def integration_marriage_sync(request: web.Request) -> web.Response:
    bot_name = _verify_integration_key(request)
    try:
        body = await request.json()
        user_id_1 = int(body["user_id_1"])
        user_id_2 = int(body["user_id_2"])
        action = str(body["action"])
        amount = int(body.get("amount") or 0)
        idem = str(body.get("idempotency_key") or _new_idempotency_key())
    except (KeyError, ValueError, TypeError, json.JSONDecodeError):
        raise web.HTTPBadRequest(reason="bad_request_body")

    if action not in ("propose", "strength", "break"):
        raise web.HTTPBadRequest(reason="unknown_action")

    status, data = await _call_economy_service(
        "/integration/marriage/sync",
        {"user_id_1": user_id_1, "user_id_2": user_id_2, "action": action,
         "amount": amount, "idempotency_key": idem, "source_bot": bot_name},
    )
    await _audit(
        bot_name, "marriage_sync",
        f"{user_id_1} + {user_id_2}, action={action} amount={amount} → {data}",
    )
    return web.json_response(data, status=status if status else 200)


@routes.post("/checker/integration/marriage/status")
async def integration_marriage_status(request: web.Request) -> web.Response:
    bot_name = _verify_integration_key(request)
    try:
        body = await request.json()
        user_id = int(body["user_id"])
    except (KeyError, ValueError, TypeError, json.JSONDecodeError):
        raise web.HTTPBadRequest(reason="bad_request_body")

    status, data = await _call_economy_service(
        "/integration/marriage/status", {"user_id": user_id, "source_bot": bot_name},
    )
    return web.json_response(data, status=status if status else 200)
