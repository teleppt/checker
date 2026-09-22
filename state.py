"""
Рантайм-состояние, которое не обязательно хранить в БД (переживать рестарт не нужно).
Сессии капчи (текущий вариант слова на каждой позиции, таймер) — в chat_captcha.py,
здесь только служебные задачи (авто-отклонение заявок) и счётчики вступлений для
антирейда.
"""
from __future__ import annotations

import asyncio
import time
from collections import defaultdict
from dataclasses import dataclass


@dataclass
class RecentJoiner:
    user_id: int
    username: str
    first_name: str
    ts: float


class RecentJoinersTracker:
    """То же самое, что JoinTracker, но хранит не только время, а и личность —
    специально для экстренного авто-бана. При настоящем рейде (сотни вступлений
    за секунды) запись в SQLite может не успевать/спотыкаться под нагрузкой, а этот
    трекер — просто список в памяти, добавление в него не может "залочиться".
    Поэтому именно отсюда, а не из базы, extreme_raid.py берёт, кого банить.
    """

    RETENTION_SECONDS = 3600

    def __init__(self) -> None:
        self._joins: dict[int, list[RecentJoiner]] = defaultdict(list)

    def register(self, chat_id: int, user_id: int, username: str, first_name: str) -> None:
        now = time.time()
        bucket = self._joins[chat_id]
        bucket.append(RecentJoiner(user_id, username, first_name, now))
        cutoff = now - self.RETENTION_SECONDS
        while bucket and bucket[0].ts < cutoff:
            bucket.pop(0)

    def since(self, chat_id: int, window_seconds: int) -> list[RecentJoiner]:
        cutoff = time.time() - window_seconds
        return [j for j in self._joins.get(chat_id, []) if j.ts >= cutoff]

    def clear_user(self, chat_id: int, user_id: int) -> None:
        bucket = self._joins.get(chat_id)
        if bucket:
            self._joins[chat_id] = [j for j in bucket if j.user_id != user_id]


recent_joiners = RecentJoinersTracker()


class JoinTracker:
    """Хранит временные метки вступлений по чатам для детекта рейда.

    Хранит дольше любого окна детекта (RETENTION_SECONDS), а не только последнее
    запрошенное окно — иначе два детектора с разными окнами (например, обычный
    антирейд на 30 сек и капча, и отдельный экстренный бан на другом окне) начинают
    друг другу мешать: тот, что вызывается первым, обрезает буфер под свой размер.
    """

    RETENTION_SECONDS = 3600

    def __init__(self) -> None:
        self._joins: dict[int, list[float]] = defaultdict(list)

    def register_join(self, chat_id: int, window_seconds: int) -> int:
        """Добавляет вступление и возвращает кол-во вступлений в окне window_seconds."""
        now = time.time()
        bucket = self._joins[chat_id]
        bucket.append(now)
        retention_cutoff = now - self.RETENTION_SECONDS
        while bucket and bucket[0] < retention_cutoff:
            bucket.pop(0)
        window_cutoff = now - window_seconds
        return len([t for t in bucket if t >= window_cutoff])

    def count(self, chat_id: int, window_seconds: int) -> int:
        now = time.time()
        cutoff = now - window_seconds
        bucket = self._joins.get(chat_id, [])
        return len([t for t in bucket if t >= cutoff])


join_tracker = JoinTracker()

# (chat_id, user_id) -> задача авто-отклонения зависшей заявки на вступление в канал
decline_tasks: dict[tuple[int, int], asyncio.Task] = {}

# (chat_id, user_id) — заявки, подтверждённые капчой до того, как approve успел отработать
verified_requests: set[tuple[int, int]] = set()

# user_id -> "group" | "channel" | "trusted" — владелец сейчас должен прислать @username/ID/пересланное
# сообщение, чтобы вручную подключить уже существующий чат или добавить доверенного (см. dashboard.py)
awaiting_registration: dict[int, str] = {}

# user_id -> chat_id — владелец сейчас должен прислать текст для поиска по участникам этого чата
awaiting_search: dict[int, int] = {}

# chat_id — для этого чата сейчас выполняется экстренный массовый бан/отклонение
# (guard от повторного запуска, пока предыдущий разбор всплеска ещё не закончился)
extreme_ban_in_progress: set[int] = set()
