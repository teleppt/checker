from __future__ import annotations

import time

from sqlalchemy import BigInteger, Boolean, Integer, String, Text, UniqueConstraint, delete, desc, event, func, or_, select
from sqlalchemy.dialects.sqlite import insert as sqlite_insert
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column

from config import config


class Base(DeclarativeBase):
    pass


class ChatSettings(Base):
    """Настройки защиты и карточка чата для админ-панели."""

    __tablename__ = "chat_settings"

    chat_id: Mapped[int] = mapped_column(BigInteger, primary_key=True)

    chat_type: Mapped[str] = mapped_column(String(16), default="other")
    title: Mapped[str] = mapped_column(String(255), default="")
    username: Mapped[str] = mapped_column(String(255), default="")

    captcha_enabled: Mapped[bool] = mapped_column(Boolean, default=False)
    captcha_manual: Mapped[bool] = mapped_column(Boolean, default=False)

    raid_active: Mapped[bool] = mapped_column(Boolean, default=False)
    raid_expires_at: Mapped[int] = mapped_column(Integer, default=0)
    raid_last_reminder_at: Mapped[int] = mapped_column(Integer, default=0)
    captcha_before_raid: Mapped[bool] = mapped_column(Boolean, default=False)

    join_blocked: Mapped[bool] = mapped_column(Boolean, default=False)
    notify_subs: Mapped[bool] = mapped_column(Boolean, default=True)

    # Антифлуд по сообщениям включён/выключен для этого чата
    antiflood_enabled: Mapped[bool] = mapped_column(Boolean, default=True)


class WatchedMember(Base):
    """Кто сейчас состоит в чате — для раздела «Список участников»."""

    __tablename__ = "watched_members"
    __table_args__ = (UniqueConstraint("chat_id", "user_id", name="uq_chat_user"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    chat_id: Mapped[int] = mapped_column(BigInteger, index=True)
    user_id: Mapped[int] = mapped_column(BigInteger)
    username: Mapped[str] = mapped_column(String(255), default="")
    first_name: Mapped[str] = mapped_column(String(255), default="")
    joined_at: Mapped[int] = mapped_column(Integer, default=0)


class TrustedUser(Base):
    """Пользователи, которым капча не показывается никогда — глобально, для всех чатов."""

    __tablename__ = "trusted_users"

    user_id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    username: Mapped[str] = mapped_column(String(255), default="")
    first_name: Mapped[str] = mapped_column(String(255), default="")
    added_at: Mapped[int] = mapped_column(Integer, default=0)


class ChatBan(Base):
    """Журнал банов по чату — для экрана «Забаненные» с возможностью разбана."""

    __tablename__ = "chat_bans"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    chat_id: Mapped[int] = mapped_column(BigInteger, index=True)
    user_id: Mapped[int] = mapped_column(BigInteger)
    username: Mapped[str] = mapped_column(String(255), default="")
    first_name: Mapped[str] = mapped_column(String(255), default="")
    banned_by: Mapped[str] = mapped_column(String(64), default="")  # ID админа или "CAS"/"эвристика"
    reason: Mapped[str] = mapped_column(String(255), default="")
    banned_at: Mapped[int] = mapped_column(Integer, default=0)


class DailyStat(Base):
    """Счётчики вступлений/выходов по дням — для раздела «Статистика»."""

    __tablename__ = "daily_stats"
    __table_args__ = (UniqueConstraint("chat_id", "date", name="uq_chat_date"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    chat_id: Mapped[int] = mapped_column(BigInteger, index=True)
    date: Mapped[str] = mapped_column(String(10))  # YYYY-MM-DD
    joins: Mapped[int] = mapped_column(Integer, default=0)
    leaves: Mapped[int] = mapped_column(Integer, default=0)


class AuditLogEntry(Base):
    """Журнал действий админов — кто что нажал и когда."""

    __tablename__ = "audit_log"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    chat_id: Mapped[int] = mapped_column(BigInteger, default=0, index=True)  # 0 — глобальное действие
    admin_id: Mapped[int] = mapped_column(BigInteger, default=0)
    action: Mapped[str] = mapped_column(String(64), default="")
    detail: Mapped[str] = mapped_column(Text, default="")
    created_at: Mapped[int] = mapped_column(Integer, default=0)


class TopicMapping(Base):
    """Соответствие 'тема лога' -> message_thread_id темы в личке с ботом (createForumTopic
    в private chat, см. topics.py). kind: "bot" (тема на клиент-бота, создаётся при первом
    обращении этого bot_name к checker'у) или "channel" (тема на канал/группу, туда падают
    подписки/отписки и прочие относящиеся к этому чату логи). key — bot_name.lower() для
    kind="bot" или chat_id (строкой) для kind="channel"."""

    __tablename__ = "topic_mappings"
    __table_args__ = (UniqueConstraint("kind", "key", name="uq_topic_kind_key"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    kind: Mapped[str] = mapped_column(String(16), index=True)
    key: Mapped[str] = mapped_column(String(255), index=True)
    thread_id: Mapped[int] = mapped_column(Integer)
    name: Mapped[str] = mapped_column(String(255), default="")
    created_at: Mapped[int] = mapped_column(Integer, default=0)


engine = create_async_engine(f"sqlite+aiosqlite:///{config.db_path}")


@event.listens_for(engine.sync_engine, "connect")
def _set_sqlite_pragma(dbapi_connection, connection_record) -> None:
    """Без этого при резком наплыве параллельных запросов (настоящий рейд — сотни
    вступлений за секунды) SQLite массово падает с 'database is locked', и часть
    записей в базу просто теряется. WAL даёт читателям работать, пока идёт запись,
    busy_timeout заставляет писателей подождать вместо немедленной ошибки."""
    cursor = dbapi_connection.cursor()
    cursor.execute("PRAGMA journal_mode=WAL")
    cursor.execute("PRAGMA busy_timeout=8000")
    cursor.close()


Session = async_sessionmaker(engine, expire_on_commit=False)


async def init_db() -> None:
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)


async def get_or_create_settings(session: AsyncSession, chat_id: int) -> ChatSettings:
    settings = await session.get(ChatSettings, chat_id)
    if settings is not None:
        return settings

    # Несколько апдейтов (join/leave/message) могут прилететь одновременно
    # на один и тот же chat_id — используем INSERT ... ON CONFLICT DO NOTHING,
    # чтобы избежать IntegrityError от гонки между корутинами.
    stmt = sqlite_insert(ChatSettings).values(chat_id=chat_id).on_conflict_do_nothing(
        index_elements=[ChatSettings.chat_id]
    )
    await session.execute(stmt)
    await session.commit()

    settings = await session.get(ChatSettings, chat_id)
    if settings is None:
        # Крайне маловероятный случай — перечитать в новом снапшоте сессии.
        await session.rollback()
        settings = await session.get(ChatSettings, chat_id)
    return settings


async def upsert_chat_info(chat_id: int, chat_type: str, title: str, username: str) -> None:
    async with Session() as session:
        settings = await get_or_create_settings(session, chat_id)
        settings.chat_type = chat_type
        settings.title = title or settings.title
        settings.username = username or settings.username
        await session.commit()


async def list_chats(chat_type: str) -> list[ChatSettings]:
    async with Session() as session:
        result = await session.execute(
            select(ChatSettings).where(ChatSettings.chat_type == chat_type).order_by(ChatSettings.title)
        )
        return list(result.scalars())


# ---------- участники ----------

async def record_member_join(chat_id: int, user_id: int, username: str, first_name: str, joined_at: int) -> None:
    async with Session() as session:
        existing = await session.execute(
            select(WatchedMember).where(WatchedMember.chat_id == chat_id, WatchedMember.user_id == user_id)
        )
        row = existing.scalar_one_or_none()
        if row is None:
            row = WatchedMember(chat_id=chat_id, user_id=user_id)
            session.add(row)
        row.username = username
        row.first_name = first_name
        row.joined_at = joined_at
        await session.commit()
    await bump_stat(chat_id, "join")


async def record_member_leave(chat_id: int, user_id: int) -> None:
    async with Session() as session:
        await session.execute(
            delete(WatchedMember).where(WatchedMember.chat_id == chat_id, WatchedMember.user_id == user_id)
        )
        await session.commit()
    await bump_stat(chat_id, "leave")


async def count_members(chat_id: int) -> int:
    async with Session() as session:
        result = await session.execute(
            select(func.count()).select_from(WatchedMember).where(WatchedMember.chat_id == chat_id)
        )
        return result.scalar_one()


async def count_members_since(chat_id: int, since_ts: int) -> int:
    async with Session() as session:
        result = await session.execute(
            select(func.count()).select_from(WatchedMember)
            .where(WatchedMember.chat_id == chat_id, WatchedMember.joined_at >= since_ts)
        )
        return result.scalar_one()


async def list_members_since(chat_id: int, since_ts: int) -> list[WatchedMember]:
    async with Session() as session:
        result = await session.execute(
            select(WatchedMember).where(WatchedMember.chat_id == chat_id, WatchedMember.joined_at >= since_ts)
        )
        return list(result.scalars())


async def list_members_since_untrusted(chat_id: int, since_ts: int) -> list[WatchedMember]:
    """Как list_members_since, но без доверенных — для массовых банов (авто-бан при
    всплеске, массовая чистка по времени), их трогать нельзя ни при каких условиях."""
    async with Session() as session:
        result = await session.execute(
            select(WatchedMember).where(
                WatchedMember.chat_id == chat_id,
                WatchedMember.joined_at >= since_ts,
                WatchedMember.user_id.not_in(select(TrustedUser.user_id)),
            )
        )
        return list(result.scalars())


async def count_members_since_untrusted(chat_id: int, since_ts: int) -> int:
    async with Session() as session:
        result = await session.execute(
            select(func.count()).select_from(WatchedMember).where(
                WatchedMember.chat_id == chat_id,
                WatchedMember.joined_at >= since_ts,
                WatchedMember.user_id.not_in(select(TrustedUser.user_id)),
            )
        )
        return result.scalar_one()


async def list_members(chat_id: int, offset: int, limit: int) -> list[WatchedMember]:
    async with Session() as session:
        result = await session.execute(
            select(WatchedMember)
            .where(WatchedMember.chat_id == chat_id)
            .order_by(WatchedMember.joined_at.desc())
            .offset(offset)
            .limit(limit)
        )
        return list(result.scalars())


async def search_members(chat_id: int, query: str, offset: int, limit: int) -> tuple[list[WatchedMember], int]:
    like = f"%{query}%"
    cond = (WatchedMember.chat_id == chat_id) & (
        WatchedMember.username.ilike(like) | WatchedMember.first_name.ilike(like)
    )
    async with Session() as session:
        total = await session.execute(select(func.count()).select_from(WatchedMember).where(cond))
        result = await session.execute(
            select(WatchedMember).where(cond).order_by(WatchedMember.joined_at.desc()).offset(offset).limit(limit)
        )
        return list(result.scalars()), total.scalar_one()


async def get_member(chat_id: int, user_id: int) -> WatchedMember | None:
    async with Session() as session:
        result = await session.execute(
            select(WatchedMember).where(WatchedMember.chat_id == chat_id, WatchedMember.user_id == user_id)
        )
        return result.scalar_one_or_none()


# ---------- доверенные пользователи (глобально) ----------

async def add_trusted(user_id: int, username: str, first_name: str) -> None:
    async with Session() as session:
        existing = await session.get(TrustedUser, user_id)
        if existing is None:
            session.add(TrustedUser(
                user_id=user_id, username=username, first_name=first_name, added_at=int(time.time())
            ))
            await session.commit()


async def remove_trusted(user_id: int) -> None:
    async with Session() as session:
        await session.execute(delete(TrustedUser).where(TrustedUser.user_id == user_id))
        await session.commit()


async def is_trusted(user_id: int) -> bool:
    async with Session() as session:
        return await session.get(TrustedUser, user_id) is not None


async def get_trusted_ids() -> set[int]:
    """Все ID доверенных одним запросом — для фильтрации перед массовыми действиями
    (авто-бан при всплеске, массовая чистка по времени), чтобы не дёргать is_trusted
    по одному на каждого потенциального кандидата на бан."""
    async with Session() as session:
        result = await session.execute(select(TrustedUser.user_id))
        return set(result.scalars())


async def list_trusted(offset: int = 0, limit: int = 100) -> list[TrustedUser]:
    async with Session() as session:
        result = await session.execute(
            select(TrustedUser).order_by(TrustedUser.added_at.desc()).offset(offset).limit(limit)
        )
        return list(result.scalars())


async def count_trusted() -> int:
    async with Session() as session:
        result = await session.execute(select(func.count()).select_from(TrustedUser))
        return result.scalar_one()


# ---------- баны ----------

async def record_ban(chat_id: int, user_id: int, username: str, first_name: str, banned_by: str, reason: str) -> None:
    async with Session() as session:
        session.add(ChatBan(
            chat_id=chat_id, user_id=user_id, username=username, first_name=first_name,
            banned_by=banned_by, reason=reason, banned_at=int(time.time()),
        ))
        await session.commit()


async def remove_ban(chat_id: int, user_id: int) -> None:
    async with Session() as session:
        await session.execute(delete(ChatBan).where(ChatBan.chat_id == chat_id, ChatBan.user_id == user_id))
        await session.commit()


async def list_bans(chat_id: int, offset: int, limit: int) -> list[ChatBan]:
    async with Session() as session:
        result = await session.execute(
            select(ChatBan).where(ChatBan.chat_id == chat_id).order_by(ChatBan.banned_at.desc())
            .offset(offset).limit(limit)
        )
        return list(result.scalars())


async def count_bans(chat_id: int) -> int:
    async with Session() as session:
        result = await session.execute(select(func.count()).select_from(ChatBan).where(ChatBan.chat_id == chat_id))
        return result.scalar_one()


# ---------- статистика по дням ----------

async def bump_stat(chat_id: int, event: str) -> None:
    date_str = time.strftime("%Y-%m-%d", time.localtime())
    async with Session() as session:
        result = await session.execute(
            select(DailyStat).where(DailyStat.chat_id == chat_id, DailyStat.date == date_str)
        )
        row = result.scalar_one_or_none()
        if row is None:
            row = DailyStat(chat_id=chat_id, date=date_str, joins=0, leaves=0)
            session.add(row)
        if event == "join":
            row.joins += 1
        else:
            row.leaves += 1
        await session.commit()


async def get_recent_stats(chat_id: int, days: int) -> list[DailyStat]:
    async with Session() as session:
        result = await session.execute(
            select(DailyStat).where(DailyStat.chat_id == chat_id).order_by(desc(DailyStat.date)).limit(days)
        )
        return list(result.scalars())


# ---------- журнал действий ----------

async def add_audit(chat_id: int, admin_id: int, action: str, detail: str = "") -> None:
    async with Session() as session:
        session.add(AuditLogEntry(
            chat_id=chat_id, admin_id=admin_id, action=action, detail=detail, created_at=int(time.time())
        ))
        await session.commit()


async def get_audit_log(chat_id: int, offset: int, limit: int) -> list[AuditLogEntry]:
    async with Session() as session:
        query = select(AuditLogEntry).order_by(AuditLogEntry.created_at.desc())
        if chat_id:
            query = query.where(AuditLogEntry.chat_id == chat_id)
        result = await session.execute(query.offset(offset).limit(limit))
        return list(result.scalars())


# ---------- темы форума в личке с ботом (см. topics.py) ----------

async def get_topic_thread(kind: str, key: str) -> int | None:
    async with Session() as session:
        result = await session.execute(
            select(TopicMapping.thread_id).where(TopicMapping.kind == kind, TopicMapping.key == key)
        )
        return result.scalar_one_or_none()


async def save_topic_thread(kind: str, key: str, thread_id: int, name: str) -> None:
    async with Session() as session:
        existing = await session.execute(
            select(TopicMapping).where(TopicMapping.kind == kind, TopicMapping.key == key)
        )
        row = existing.scalar_one_or_none()
        if row is None:
            session.add(TopicMapping(
                kind=kind, key=key, thread_id=thread_id, name=name, created_at=int(time.time())
            ))
        else:
            row.thread_id = thread_id
            row.name = name
        try:
            await session.commit()
        except IntegrityError:
            # Гонка: две параллельные проверки для одного и того же bot_name/канала
            # обе не нашли тему в кэше и обе создали её в Telegram почти одновременно —
            # тут просто откатываемся, в БД остаётся первая запись (topics.py разберётся
            # с двумя реальными темами в Telegram сам, при следующем обращении подтянет
            # ту, что сохранена в БД).
            await session.rollback()


async def list_topics(kind: str | None = None) -> list[TopicMapping]:
    async with Session() as session:
        query = select(TopicMapping).order_by(TopicMapping.created_at)
        if kind:
            query = query.where(TopicMapping.kind == kind)
        result = await session.execute(query)
        return list(result.scalars())
