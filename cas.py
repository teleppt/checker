from __future__ import annotations

import logging

import aiohttp

from config import config

logger = logging.getLogger(__name__)

# Один общий session/connector на всё приложение, а не по одному на каждую проверку.
# При рейде на сотни вступлений одновременно, создание отдельной TCP-сессии на каждый
# запрос увеличивает шанс таймаутов/отказов у самого api.cas.chat — лимит на количество
# одновременных соединений (limit=30) не даёт заспамить его до отказа.
_session: aiohttp.ClientSession | None = None


def _get_session() -> aiohttp.ClientSession:
    global _session
    if _session is None or _session.closed:
        timeout = aiohttp.ClientTimeout(total=config.cas_timeout_seconds)
        connector = aiohttp.TCPConnector(limit=30)
        _session = aiohttp.ClientSession(timeout=timeout, connector=connector)
    return _session


async def is_banned(user_id: int) -> bool:
    """True, если ID числится в базе известных спам-ботов CAS (cas.chat)."""
    if not config.cas_enabled:
        return False
    try:
        session = _get_session()
        async with session.get(config.cas_api_url, params={"user_id": user_id}) as resp:
            if resp.status != 200:
                return False
            data = await resp.json(content_type=None)
            return bool(data.get("ok"))
    except Exception:
        logger.warning("CAS check failed for user %s", user_id, exc_info=True)
        return False  # сеть недоступна/таймаут/лимит соединений — не блокируем человека из-за нашей ошибки
