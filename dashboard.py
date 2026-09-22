from __future__ import annotations

import asyncio
import time

from aiogram import Bot, F, Router
from aiogram.enums import ChatType
from aiogram.exceptions import TelegramBadRequest
from aiogram.types import CallbackQuery, InlineKeyboardButton, InlineKeyboardMarkup, Message

import db
import logic
import state

router = Router(name="dashboard")

PAGE_SIZE = 8

CATEGORY_TITLES = {
    "channel": "Каналы",
    "group": "Группы",
    "other": "Другое",
}


def _root_markup() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[[
        InlineKeyboardButton(text="Каналы", callback_data="dash:cat:channel:0"),
        InlineKeyboardButton(text="Группы", callback_data="dash:cat:group:0"),
        InlineKeyboardButton(text="Другое", callback_data="dash:cat:other:0"),
    ]])


async def render_root() -> tuple[str, InlineKeyboardMarkup]:
    return "<b>Меню управления</b>\n\nВыбери раздел:", _root_markup()


async def render_category(chat_type: str, page: int) -> tuple[str, InlineKeyboardMarkup]:
    if chat_type == "other":
        text = (
            "<b>Другое</b>\n\n"
            "Здесь — общие штуки бота, не привязанные к конкретному чату."
        )
        kb = InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="Добавить в группу", url="https://t.me/{me}?startgroup=true")],
            [InlineKeyboardButton(text="Добавить в канал", url="https://t.me/{me}?startchannel=true")],
            [InlineKeyboardButton(text="Доверенные пользователи", callback_data="dash:trusted:0")],
            [InlineKeyboardButton(text="Как это работает", callback_data="dash:help")],
            [InlineKeyboardButton(text="Главное меню", callback_data="dash:root")],
        ])
        return text, kb  # url с {me} подставится в cb_category перед отправкой

    chats = await db.list_chats(chat_type)
    title = CATEGORY_TITLES[chat_type]

    if not chats:
        text = f"{title}\n\nПока пусто — добавь бота админом в чат, и он появится тут."
        add_label = "Добавить канал" if chat_type == "channel" else "Добавить группу"
        kb = InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text=add_label, callback_data=f"dash:add:{chat_type}")],
            [InlineKeyboardButton(text="Главное меню", callback_data="dash:root")],
        ])
        return text, kb

    total_pages = max(1, (len(chats) + PAGE_SIZE - 1) // PAGE_SIZE)
    page = max(0, min(page, total_pages - 1))
    chunk = chats[page * PAGE_SIZE : page * PAGE_SIZE + PAGE_SIZE]

    rows = []
    for settings in chunk:
        emoji = logic.status_emoji(chat_type, settings)
        name = logic.chat_display_name(settings)
        rows.append([InlineKeyboardButton(text=f"{name} {emoji}", callback_data=f"dash:chat:{settings.chat_id}")])

    if total_pages > 1:
        nav = []
        if page > 0:
            nav.append(InlineKeyboardButton(text="<", callback_data=f"dash:cat:{chat_type}:{page - 1}"))
        nav.append(InlineKeyboardButton(text=f"{page + 1}/{total_pages}", callback_data="dash:noop"))
        if page < total_pages - 1:
            nav.append(InlineKeyboardButton(text=">", callback_data=f"dash:cat:{chat_type}:{page + 1}"))
        rows.append(nav)

    add_label = "Добавить канал" if chat_type == "channel" else "Добавить группу"
    rows.append([InlineKeyboardButton(text=add_label, callback_data=f"dash:add:{chat_type}")])
    rows.append([InlineKeyboardButton(text="Главное меню", callback_data="dash:root")])
    return f"{title}\n\nВыбери чат:", InlineKeyboardMarkup(inline_keyboard=rows)


async def render_chat(chat_id: int) -> tuple[str, InlineKeyboardMarkup] | None:
    settings = await logic.get_settings_snapshot(chat_id)
    if settings.chat_type not in ("group", "channel"):
        return None

    emoji = logic.status_emoji(settings.chat_type, settings)
    name = logic.chat_display_name(settings)
    activity = await logic.status_text(chat_id)
    members_count = await db.count_members(chat_id)
    bans_count = await db.count_bans(chat_id)

    text = (
        f"{emoji} <b>{name}</b>\n\n"
        f"{activity}\n\n"
        f"Уведомления о подписках: {'вкл' if settings.notify_subs else 'выкл'}\n"
        f"Антифлуд по сообщениям: {'вкл' if settings.antiflood_enabled else 'выкл'}\n"
        f"Участников на учёте: {members_count} · забанено: {bans_count}"
    )

    rows = []
    if settings.chat_type == "group":
        rows.append([InlineKeyboardButton(
            text="Выключить капчу" if settings.captcha_enabled else "Включить капчу",
            callback_data=f"dash:chat:{chat_id}:captcha_off" if settings.captcha_enabled else f"dash:chat:{chat_id}:captcha_on",
        )])

    rows.append([InlineKeyboardButton(
        text="Открыть доступ" if settings.join_blocked else "Запретить вход всем",
        callback_data=f"dash:chat:{chat_id}:block_off" if settings.join_blocked else f"dash:chat:{chat_id}:block_on",
    )])
    rows.append([InlineKeyboardButton(
        text="Выключить уведомления" if settings.notify_subs else "Включить уведомления",
        callback_data=f"dash:chat:{chat_id}:notify_off" if settings.notify_subs else f"dash:chat:{chat_id}:notify_on",
    )])
    if settings.chat_type == "group":
        rows.append([InlineKeyboardButton(
            text="Выключить антифлуд" if settings.antiflood_enabled else "Включить антифлуд",
            callback_data=f"dash:chat:{chat_id}:flood_off" if settings.antiflood_enabled else f"dash:chat:{chat_id}:flood_on",
        )])
    if settings.raid_active:
        rows.append([InlineKeyboardButton(text="Снять режим рейда", callback_data=f"dash:chat:{chat_id}:raid_off")])

    if settings.chat_type == "channel":
        rows.append([InlineKeyboardButton(text="Ссылка для входа через бота", callback_data=f"dash:chat:{chat_id}:get_link")])

    rows.append([InlineKeyboardButton(text=f"Участники ({members_count})", callback_data=f"dash:members:{chat_id}:0"),
                 InlineKeyboardButton(text=f"Забаненные ({bans_count})", callback_data=f"dash:bans:{chat_id}:0")])
    rows.append([InlineKeyboardButton(text="Статистика", callback_data=f"dash:stats:{chat_id}"),
                 InlineKeyboardButton(text="Журнал", callback_data=f"dash:log:{chat_id}:0")])
    rows.append([InlineKeyboardButton(text="Массовый бан по времени вступления", callback_data=f"dash:purge:{chat_id}")])
    rows.append([
        InlineKeyboardButton(text="К списку", callback_data=f"dash:cat:{settings.chat_type}:0"),
        InlineKeyboardButton(text="Главное меню", callback_data="dash:root"),
    ])
    return text, InlineKeyboardMarkup(inline_keyboard=rows)


PURGE_WINDOWS: list[tuple[str, int]] = [
    ("30 минут", 30),
    ("1 час", 60),
    ("2 часа", 120),
    ("3 часа", 180),
    ("6 часов", 360),
    ("12 часов", 720),
    ("24 часа", 1440),
]


async def render_purge_menu(chat_id: int) -> tuple[str, InlineKeyboardMarkup]:
    settings = await logic.get_settings_snapshot(chat_id)
    name = logic.chat_display_name(settings)
    now = int(time.time())

    lines = [
        f"<b>{name}</b>", "",
        "Забанит и удалит из чата всех, кто вступил за выбранный период (доверенных не тронет):",
    ]
    rows = []
    for label, minutes in PURGE_WINDOWS:
        count = await db.count_members_since_untrusted(chat_id, now - minutes * 60)
        rows.append([InlineKeyboardButton(
            text=f"{label} ({count})", callback_data=f"dash:purge:{chat_id}:{minutes}"
        )])

    rows.append([InlineKeyboardButton(text="К чату", callback_data=f"dash:chat:{chat_id}")])
    return "\n".join(lines), InlineKeyboardMarkup(inline_keyboard=rows)


async def render_purge_confirm(chat_id: int, minutes: int) -> tuple[str, InlineKeyboardMarkup]:
    settings = await logic.get_settings_snapshot(chat_id)
    name = logic.chat_display_name(settings)
    label = next((l for l, m in PURGE_WINDOWS if m == minutes), f"{minutes} мин")
    count = await db.count_members_since_untrusted(chat_id, int(time.time()) - minutes * 60)

    if count == 0:
        text = f"<b>{name}</b>\n\nЗа последние {label} никто не вступал — банить некого."
        kb = InlineKeyboardMarkup(inline_keyboard=[[
            InlineKeyboardButton(text="К списку периодов", callback_data=f"dash:purge:{chat_id}")
        ]])
        return text, kb

    text = (
        f"<b>{name}</b>\n\n"
        f"Забанить и удалить {count} чел., вступивших за последние {label}?\n"
        f"Отменить это будет нельзя — банит по одному через Telegram API, на большое число "
        f"человек может занять время."
    )
    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text=f"Да, забанить {count} чел.", callback_data=f"dash:purge:{chat_id}:{minutes}:go")],
        [InlineKeyboardButton(text="Отмена", callback_data=f"dash:purge:{chat_id}")],
    ])
    return text, kb


async def render_bans(chat_id: int, page: int) -> tuple[str, InlineKeyboardMarkup]:
    settings = await logic.get_settings_snapshot(chat_id)
    total = await db.count_bans(chat_id)
    name = logic.chat_display_name(settings)

    if total == 0:
        text = f"<b>{name}</b>\n\nЗабаненных нет."
        kb = InlineKeyboardMarkup(inline_keyboard=[[
            InlineKeyboardButton(text="К чату", callback_data=f"dash:chat:{chat_id}")
        ]])
        return text, kb

    total_pages = max(1, (total + PAGE_SIZE - 1) // PAGE_SIZE)
    page = max(0, min(page, total_pages - 1))
    bans = await db.list_bans(chat_id, offset=page * PAGE_SIZE, limit=PAGE_SIZE)

    lines = [f"<b>{name}</b>\n"]
    rows = []
    for b in bans:
        who = f"@{b.username}" if b.username else (b.first_name or str(b.user_id))
        when = time.strftime("%d.%m.%Y", time.localtime(b.banned_at)) if b.banned_at else "—"
        lines.append(f"• {who} — {when}, причина: {b.reason or '—'}")
        rows.append([InlineKeyboardButton(
            text=f"Разбанить {who}", callback_data=f"dash:unban:{chat_id}:{b.user_id}:{page}"
        )])

    if total_pages > 1:
        nav = []
        if page > 0:
            nav.append(InlineKeyboardButton(text="<", callback_data=f"dash:bans:{chat_id}:{page - 1}"))
        nav.append(InlineKeyboardButton(text=f"{page + 1}/{total_pages}", callback_data="dash:noop"))
        if page < total_pages - 1:
            nav.append(InlineKeyboardButton(text=">", callback_data=f"dash:bans:{chat_id}:{page + 1}"))
        rows.append(nav)

    rows.append([InlineKeyboardButton(text="К чату", callback_data=f"dash:chat:{chat_id}")])
    return "\n".join(lines), InlineKeyboardMarkup(inline_keyboard=rows)


async def render_stats(chat_id: int) -> tuple[str, InlineKeyboardMarkup]:
    settings = await logic.get_settings_snapshot(chat_id)
    name = logic.chat_display_name(settings)
    stats = await db.get_recent_stats(chat_id, 7)
    by_date = {s.date: s for s in stats}

    lines = [f"<b>{name}</b> — последние 7 дней\n"]
    today = time.localtime()
    max_val = max([max(s.joins, s.leaves) for s in stats], default=1) or 1
    for i in range(6, -1, -1):
        ts = time.mktime(today) - i * 86400
        date_str = time.strftime("%Y-%m-%d", time.localtime(ts))
        label = time.strftime("%d.%m", time.localtime(ts))
        s = by_date.get(date_str)
        joins, leaves = (s.joins, s.leaves) if s else (0, 0)
        bar = "█" * min(10, round(joins / max_val * 10)) or ("·" if joins == 0 else "█")
        lines.append(f"{label}: +{joins} / -{leaves}  {bar}")

    kb = InlineKeyboardMarkup(inline_keyboard=[[
        InlineKeyboardButton(text="К чату", callback_data=f"dash:chat:{chat_id}")
    ]])
    return "\n".join(lines), kb


async def render_audit_log(chat_id: int, page: int) -> tuple[str, InlineKeyboardMarkup]:
    settings = await logic.get_settings_snapshot(chat_id)
    name = logic.chat_display_name(settings)
    entries = await db.get_audit_log(chat_id, offset=page * PAGE_SIZE, limit=PAGE_SIZE)

    if not entries and page == 0:
        text = f"<b>{name}</b>\n\nПока пусто."
    else:
        lines = [f"<b>{name}</b>\n"]
        action_labels = {
            "captcha_on": "включил капчу", "captcha_off": "выключил капчу",
            "block_on": "закрыл вход всем", "block_off": "открыл доступ",
            "antiflood_on": "включил антифлуд", "antiflood_off": "выключил антифлуд",
            "raid_off": "снял режим рейда", "kick": "удалил участника",
            "ban": "забанил участника", "unban": "разбанил участника",
            "cas_ban": "авто-бан по CAS", "antiflood_mute": "авто-мут за флуд",
            "mass_ban": "массовый бан по времени вступления",
        }
        for e in entries:
            when = time.strftime("%d.%m %H:%M", time.localtime(e.created_at))
            who = "бот (авто)" if not e.admin_id else f"admin {e.admin_id}"
            label = action_labels.get(e.action, e.action)
            lines.append(f"• {when} — {who}: {label} {e.detail}".rstrip())
        text = "\n".join(lines)

    rows = []
    if len(entries) == PAGE_SIZE:
        rows.append([InlineKeyboardButton(text="Ещё", callback_data=f"dash:log:{chat_id}:{page + 1}")])
    elif page > 0:
        rows.append([InlineKeyboardButton(text="Назад", callback_data=f"dash:log:{chat_id}:{page - 1}")])
    rows.append([InlineKeyboardButton(text="К чату", callback_data=f"dash:chat:{chat_id}")])
    return text, InlineKeyboardMarkup(inline_keyboard=rows)


async def render_trusted(page: int) -> tuple[str, InlineKeyboardMarkup]:
    total = await db.count_trusted()
    if total == 0:
        text = "<b>Доверенные</b>\n\nПока никого нет."
        kb = InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="Добавить доверенного", callback_data="dash:add:trusted")],
            [InlineKeyboardButton(text="Другое", callback_data="dash:cat:other:0")],
        ])
        return text, kb

    total_pages = max(1, (total + PAGE_SIZE - 1) // PAGE_SIZE)
    page = max(0, min(page, total_pages - 1))
    users = await db.list_trusted(offset=page * PAGE_SIZE, limit=PAGE_SIZE)

    rows = []
    for u in users:
        label = f"@{u.username}" if u.username else (u.first_name or str(u.user_id))
        rows.append([InlineKeyboardButton(text=label, callback_data=f"dash:untrust:{u.user_id}:{page}")])

    if total_pages > 1:
        nav = []
        if page > 0:
            nav.append(InlineKeyboardButton(text="<", callback_data=f"dash:trusted:{page - 1}"))
        nav.append(InlineKeyboardButton(text=f"{page + 1}/{total_pages}", callback_data="dash:noop"))
        if page < total_pages - 1:
            nav.append(InlineKeyboardButton(text=">", callback_data=f"dash:trusted:{page + 1}"))
        rows.append(nav)

    rows.append([InlineKeyboardButton(text="Добавить доверенного", callback_data="dash:add:trusted")])
    rows.append([InlineKeyboardButton(text="Другое", callback_data="dash:cat:other:0")])
    return f"<b>Доверенные</b> ({total})\n\nНажми, чтобы убрать из списка:", InlineKeyboardMarkup(inline_keyboard=rows)


async def render_members(chat_id: int, page: int) -> tuple[str, InlineKeyboardMarkup]:
    settings = await logic.get_settings_snapshot(chat_id)
    total = await db.count_members(chat_id)
    name = logic.chat_display_name(settings)

    if total == 0:
        text = f"<b>{name}</b>\n\nПока никого нет на учёте."
        kb = InlineKeyboardMarkup(inline_keyboard=[[
            InlineKeyboardButton(text="К чату", callback_data=f"dash:chat:{chat_id}")
        ]])
        return text, kb

    total_pages = max(1, (total + PAGE_SIZE - 1) // PAGE_SIZE)
    page = max(0, min(page, total_pages - 1))
    members = await db.list_members(chat_id, offset=page * PAGE_SIZE, limit=PAGE_SIZE)

    rows = []
    for m in members:
        label = f"@{m.username}" if m.username else (m.first_name or str(m.user_id))
        rows.append([InlineKeyboardButton(text=label, callback_data=f"dash:member:{chat_id}:{m.user_id}:0")])

    if total_pages > 1:
        nav = []
        if page > 0:
            nav.append(InlineKeyboardButton(text="<", callback_data=f"dash:members:{chat_id}:{page - 1}"))
        nav.append(InlineKeyboardButton(text=f"{page + 1}/{total_pages}", callback_data="dash:noop"))
        if page < total_pages - 1:
            nav.append(InlineKeyboardButton(text=">", callback_data=f"dash:members:{chat_id}:{page + 1}"))
        rows.append(nav)

    rows.append([InlineKeyboardButton(text="Поиск", callback_data=f"dash:search:{chat_id}")])
    rows.append([InlineKeyboardButton(text="К чату", callback_data=f"dash:chat:{chat_id}")])
    return f"<b>{name}</b>\n\nУчастников: {total}", InlineKeyboardMarkup(inline_keyboard=rows)


async def render_search_results(chat_id: int, query: str, page: int) -> tuple[str, InlineKeyboardMarkup]:
    settings = await logic.get_settings_snapshot(chat_id)
    name = logic.chat_display_name(settings)
    members, total = await db.search_members(chat_id, query, offset=page * PAGE_SIZE, limit=PAGE_SIZE)

    if total == 0:
        text = f"<b>{name}</b>\n\nПо запросу «{query}» никого не нашлось."
        kb = InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="Искать ещё раз", callback_data=f"dash:search:{chat_id}")],
            [InlineKeyboardButton(text="К чату", callback_data=f"dash:chat:{chat_id}")],
        ])
        return text, kb

    rows = []
    for m in members:
        label = f"@{m.username}" if m.username else (m.first_name or str(m.user_id))
        rows.append([InlineKeyboardButton(text=label, callback_data=f"dash:member:{chat_id}:{m.user_id}:0")])

    rows.append([InlineKeyboardButton(text="Искать ещё раз", callback_data=f"dash:search:{chat_id}")])
    rows.append([InlineKeyboardButton(text="К чату", callback_data=f"dash:chat:{chat_id}")])
    return f"<b>{name}</b>\n\nНайдено по «{query}»: {total}", InlineKeyboardMarkup(inline_keyboard=rows)


async def render_member(chat_id: int, user_id: int, back_page: int) -> tuple[str, InlineKeyboardMarkup] | None:
    member = await db.get_member(chat_id, user_id)
    if member is None:
        return None

    joined = time.strftime("%d.%m.%Y %H:%M", time.localtime(member.joined_at)) if member.joined_at else "—"
    text = (
        f"<b>{member.first_name or '—'}</b>\n"
        f"Юзернейм: {'@' + member.username if member.username else '—'}\n"
        f"ID: <code>{member.user_id}</code>\n"
        f"Вступил: {joined}"
    )

    profile_url = f"https://t.me/{member.username}" if member.username else f"tg://user?id={member.user_id}"
    rows = [
        [InlineKeyboardButton(text="Перейти в профиль", url=profile_url)],
        [InlineKeyboardButton(text="Удалить", callback_data=f"dash:member:{chat_id}:{user_id}:{back_page}:kick")],
        [InlineKeyboardButton(text="Забанить", callback_data=f"dash:member:{chat_id}:{user_id}:{back_page}:ban")],
        [InlineKeyboardButton(text="К списку", callback_data=f"dash:members:{chat_id}:{back_page}")],
    ]
    return text, InlineKeyboardMarkup(inline_keyboard=rows)


HELP_TEXT = (
    "Я слежу за резкими всплесками вступлений в группе/канале (похоже на накрутку) "
    "и умею включать капчу на вход — сам или по твоей команде.\n\n"
    "Капча — это веб-страничка внутри Telegram (Mini App), а не кнопки в чате.\n\n"
    "В «Каналы» и «Группы» — список чатов, где я админ, с их статусом. В карточке чата "
    "можно включить/выключить капчу, полностью закрыть вход, управлять уведомлениями "
    "и смотреть/чистить список участников."
)


async def _safe_edit(call: CallbackQuery, text: str, kb: InlineKeyboardMarkup) -> None:
    try:
        await call.message.edit_text(text, reply_markup=kb)
    except TelegramBadRequest:
        pass


@router.callback_query(F.data == "dash:root")
async def cb_root(call: CallbackQuery) -> None:
    if not logic.is_admin(call.from_user.id):
        await call.answer("Только для владельца.", show_alert=True)
        return
    text, kb = await render_root()
    await _safe_edit(call, text, kb)
    await call.answer()


@router.callback_query(F.data == "dash:noop")
async def cb_noop(call: CallbackQuery) -> None:
    await call.answer()


@router.callback_query(F.data == "dash:help")
async def cb_help(call: CallbackQuery) -> None:
    if not logic.is_admin(call.from_user.id):
        await call.answer("Только для владельца.", show_alert=True)
        return
    kb = InlineKeyboardMarkup(inline_keyboard=[[
        InlineKeyboardButton(text="Главное меню", callback_data="dash:root")
    ]])
    await _safe_edit(call, HELP_TEXT, kb)
    await call.answer()


@router.callback_query(F.data.startswith("dash:cat:"))
async def cb_category(call: CallbackQuery, bot: Bot) -> None:
    if not logic.is_admin(call.from_user.id):
        await call.answer("Только для владельца.", show_alert=True)
        return
    _, _, chat_type, page_str = call.data.split(":")
    text, kb = await render_category(chat_type, int(page_str))
    if chat_type == "other":
        me = await bot.get_me()
        for row in kb.inline_keyboard:
            for btn in row:
                if btn.url and "{me}" in btn.url:
                    btn.url = btn.url.replace("{me}", me.username)
    await _safe_edit(call, text, kb)
    await call.answer()


@router.callback_query(F.data.startswith("dash:chat:"))
async def cb_chat(call: CallbackQuery, bot: Bot) -> None:
    if not logic.is_admin(call.from_user.id):
        await call.answer("Только для владельца.", show_alert=True)
        return

    parts = call.data.split(":")
    chat_id = int(parts[2])
    action = parts[3] if len(parts) > 3 else None

    if action == "captcha_on":
        await logic.set_captcha(chat_id, True)
        await db.add_audit(chat_id, call.from_user.id, "captcha_on")
        await call.answer("Капча включена")
    elif action == "captcha_off":
        await logic.set_captcha(chat_id, False)
        await db.add_audit(chat_id, call.from_user.id, "captcha_off")
        await call.answer("Капча выключена")
    elif action == "block_on":
        await logic.set_join_blocked(chat_id, True)
        await db.add_audit(chat_id, call.from_user.id, "block_on")
        await call.answer("Вход закрыт всем")
    elif action == "block_off":
        await logic.set_join_blocked(chat_id, False)
        await db.add_audit(chat_id, call.from_user.id, "block_off")
        await call.answer("Доступ открыт")
    elif action == "notify_on":
        await logic.set_notify_subs(chat_id, True)
        await call.answer("Уведомления включены")
    elif action == "notify_off":
        await logic.set_notify_subs(chat_id, False)
        await call.answer("Уведомления выключены")
    elif action == "flood_on":
        await logic.set_antiflood(chat_id, True)
        await db.add_audit(chat_id, call.from_user.id, "antiflood_on")
        await call.answer("Антифлуд включён")
    elif action == "flood_off":
        await logic.set_antiflood(chat_id, False)
        await db.add_audit(chat_id, call.from_user.id, "antiflood_off")
        await call.answer("Антифлуд выключен")
    elif action == "raid_off":
        was_active = await logic.turn_off_raid(chat_id)
        if was_active:
            await db.add_audit(chat_id, call.from_user.id, "raid_off")
        await call.answer("Режим рейда снят" if was_active else "Рейд и так не активен")
    elif action == "get_link":
        me = await bot.get_me()
        link = f"https://t.me/{me.username}?start=join_{chat_id}"
        await call.answer()
        await call.message.answer(
            "Персональная ссылка для входа через бота — раздай её вместо обычной "
            "ссылки-приглашения на канал (в описании, в закрепе и т.п.):\n\n"
            f"{link}\n\n"
            "Она сама проведёт человека через веб-капчу и выдаст ему одноразовую ссылку "
            "на канал только после проверки."
        )
    else:
        await call.answer()

    result = await render_chat(chat_id)
    if result is None:
        await call.answer("Чат не найден.", show_alert=True)
        return
    text, kb = result
    await _safe_edit(call, text, kb)


@router.callback_query(F.data.startswith("dash:members:"))
async def cb_members(call: CallbackQuery) -> None:
    if not logic.is_admin(call.from_user.id):
        await call.answer("Только для владельца.", show_alert=True)
        return
    _, _, chat_id_str, page_str = call.data.split(":")
    text, kb = await render_members(int(chat_id_str), int(page_str))
    await _safe_edit(call, text, kb)
    await call.answer()


@router.callback_query(F.data.startswith("dash:member:"))
async def cb_member(call: CallbackQuery, bot: Bot) -> None:
    if not logic.is_admin(call.from_user.id):
        await call.answer("Только для владельца.", show_alert=True)
        return

    parts = call.data.split(":")
    chat_id, user_id, back_page = int(parts[2]), int(parts[3]), int(parts[4])
    action = parts[5] if len(parts) > 5 else None

    if action == "kick":
        member = await db.get_member(chat_id, user_id)
        try:
            await bot.ban_chat_member(chat_id, user_id)
            await bot.unban_chat_member(chat_id, user_id, only_if_banned=True)
        except TelegramBadRequest:
            pass
        await db.record_member_leave(chat_id, user_id)
        await db.add_audit(chat_id, call.from_user.id, "kick", f"user={user_id}")
        await call.answer("Удалён из чата.")
        text, kb = await render_members(chat_id, back_page)
        await _safe_edit(call, text, kb)
        return

    if action == "ban":
        member = await db.get_member(chat_id, user_id)
        try:
            await bot.ban_chat_member(chat_id, user_id)
        except TelegramBadRequest:
            pass
        await db.record_member_leave(chat_id, user_id)
        await db.record_ban(
            chat_id, user_id,
            member.username if member else "", member.first_name if member else "",
            str(call.from_user.id), "вручную из панели",
        )
        await db.add_audit(chat_id, call.from_user.id, "ban", f"user={user_id}")
        await call.answer("Забанен")
        text, kb = await render_members(chat_id, back_page)
        await _safe_edit(call, text, kb)
        return

    result = await render_member(chat_id, user_id, back_page)
    if result is None:
        await call.answer("Участник не найден (мог уже выйти).", show_alert=True)
        return
    text, kb = result
    await _safe_edit(call, text, kb)
    await call.answer()


@router.callback_query(F.data.startswith("dash:purge:"))
async def cb_purge(call: CallbackQuery, bot: Bot) -> None:
    if not logic.is_admin(call.from_user.id):
        await call.answer("Только для владельца.", show_alert=True)
        return

    parts = call.data.split(":")
    chat_id = int(parts[2])

    if len(parts) == 3:
        text, kb = await render_purge_menu(chat_id)
        await _safe_edit(call, text, kb)
        await call.answer()
        return

    minutes = int(parts[3])

    if len(parts) == 4:
        text, kb = await render_purge_confirm(chat_id, minutes)
        await _safe_edit(call, text, kb)
        await call.answer()
        return

    # parts[4] == "go" — подтверждено, запускаем массовый бан в фоне
    since_ts = int(time.time()) - minutes * 60
    members = await db.list_members_since_untrusted(chat_id, since_ts)
    label = next((l for l, m in PURGE_WINDOWS if m == minutes), f"{minutes} мин")

    if not members:
        await call.answer("Уже некого банить.", show_alert=True)
        text, kb = await render_purge_menu(chat_id)
        await _safe_edit(call, text, kb)
        return

    await call.answer(f"Начал банить {len(members)} чел.")
    await _safe_edit(
        call,
        f"Баню {len(members)} чел., вступивших за последние {label}. Отчёт придёт отдельным сообщением.",
        InlineKeyboardMarkup(inline_keyboard=[[
            InlineKeyboardButton(text="К чату", callback_data=f"dash:chat:{chat_id}")
        ]]),
    )
    asyncio.create_task(
        _run_purge(bot, chat_id, members, label, call.from_user.id, call.message.chat.id)
    )


async def _run_purge(bot: Bot, chat_id: int, members: list, label: str, admin_id: int, report_chat_id: int) -> None:
    banned = 0
    failed = 0
    for m in members:
        try:
            await bot.ban_chat_member(chat_id, m.user_id)
            await db.record_ban(
                chat_id, m.user_id, m.username, m.first_name,
                str(admin_id), f"массовая чистка: вступил за последние {label}",
            )
            await db.record_member_leave(chat_id, m.user_id)
            banned += 1
        except TelegramBadRequest:
            failed += 1
        await asyncio.sleep(0.05)  # не долбить Telegram API пачкой без пауз

    await db.add_audit(chat_id, admin_id, "mass_ban", f"window={label} banned={banned} failed={failed}")

    settings = await logic.get_settings_snapshot(chat_id)
    name = logic.chat_display_name(settings)
    report = f"Готово: «{name}» — забанено {banned} чел. за последние {label}"
    if failed:
        report += f", не получилось у {failed} (могли уже выйти сами)."
    else:
        report += "."
    try:
        await bot.send_message(report_chat_id, report)
    except TelegramBadRequest:
        pass


@router.callback_query(F.data.startswith("dash:bans:"))
async def cb_bans(call: CallbackQuery) -> None:
    if not logic.is_admin(call.from_user.id):
        await call.answer("Только для владельца.", show_alert=True)
        return
    _, _, chat_id_str, page_str = call.data.split(":")
    text, kb = await render_bans(int(chat_id_str), int(page_str))
    await _safe_edit(call, text, kb)
    await call.answer()


@router.callback_query(F.data.startswith("dash:unban:"))
async def cb_unban(call: CallbackQuery, bot: Bot) -> None:
    if not logic.is_admin(call.from_user.id):
        await call.answer("Только для владельца.", show_alert=True)
        return
    _, _, chat_id_str, user_id_str, page_str = call.data.split(":")
    chat_id, user_id, page = int(chat_id_str), int(user_id_str), int(page_str)
    try:
        await bot.unban_chat_member(chat_id, user_id, only_if_banned=True)
    except TelegramBadRequest:
        pass
    await db.remove_ban(chat_id, user_id)
    await db.add_audit(chat_id, call.from_user.id, "unban", f"user={user_id}")
    await call.answer("Разбанен")
    text, kb = await render_bans(chat_id, page)
    await _safe_edit(call, text, kb)


@router.callback_query(F.data.startswith("dash:stats:"))
async def cb_stats(call: CallbackQuery) -> None:
    if not logic.is_admin(call.from_user.id):
        await call.answer("Только для владельца.", show_alert=True)
        return
    chat_id = int(call.data.split(":")[2])
    text, kb = await render_stats(chat_id)
    await _safe_edit(call, text, kb)
    await call.answer()


@router.callback_query(F.data.startswith("dash:log:"))
async def cb_log(call: CallbackQuery) -> None:
    if not logic.is_admin(call.from_user.id):
        await call.answer("Только для владельца.", show_alert=True)
        return
    _, _, chat_id_str, page_str = call.data.split(":")
    text, kb = await render_audit_log(int(chat_id_str), int(page_str))
    await _safe_edit(call, text, kb)
    await call.answer()


@router.callback_query(F.data.startswith("dash:trusted:"))
async def cb_trusted(call: CallbackQuery) -> None:
    if not logic.is_admin(call.from_user.id):
        await call.answer("Только для владельца.", show_alert=True)
        return
    page = int(call.data.split(":")[2])
    text, kb = await render_trusted(page)
    await _safe_edit(call, text, kb)
    await call.answer()


@router.callback_query(F.data.startswith("dash:untrust:"))
async def cb_untrust(call: CallbackQuery) -> None:
    if not logic.is_admin(call.from_user.id):
        await call.answer("Только для владельца.", show_alert=True)
        return
    _, _, user_id_str, page_str = call.data.split(":")
    user_id, page = int(user_id_str), int(page_str)
    await db.remove_trusted(user_id)
    await db.add_audit(0, call.from_user.id, "untrust", f"user={user_id}")
    await call.answer("Убрал из доверенных.")
    text, kb = await render_trusted(page)
    await _safe_edit(call, text, kb)


# ---------- поиск по участникам ----------

@router.callback_query(F.data.startswith("dash:search:"))
async def cb_search_start(call: CallbackQuery) -> None:
    if not logic.is_admin(call.from_user.id):
        await call.answer("Только для владельца.", show_alert=True)
        return
    chat_id = int(call.data.split(":")[2])
    state.awaiting_search[call.from_user.id] = chat_id
    kb = InlineKeyboardMarkup(inline_keyboard=[[
        InlineKeyboardButton(text="Отмена", callback_data=f"dash:members:{chat_id}:0")
    ]])
    await call.message.edit_text("Пришли часть юзернейма или имени для поиска:", reply_markup=kb)
    await call.answer()


def _is_awaiting_search(message: Message) -> bool:
    return message.from_user is not None and message.from_user.id in state.awaiting_search


@router.message(F.chat.type == ChatType.PRIVATE, _is_awaiting_search)
async def handle_search_reply(message: Message) -> None:
    chat_id = state.awaiting_search.pop(message.from_user.id, None)
    if chat_id is None:
        return
    query = (message.text or "").strip()
    if not query:
        state.awaiting_search[message.from_user.id] = chat_id
        await message.answer("Пусто — пришли хотя бы пару символов для поиска.")
        return
    text, kb = await render_search_results(chat_id, query, 0)
    await message.answer(text, reply_markup=kb)


# ---------- ручная привязка уже существующего чата ----------
# Нужна, потому что бот узнаёт о новом чате только из события смены своего статуса
# (my_chat_member) — если бота добавили ДО этого кода или база сбросилась при
# редеплое без Volume, чат просто не появится сам, событие повторно не придёт.

def _is_awaiting_registration(message: Message) -> bool:
    return message.from_user is not None and message.from_user.id in state.awaiting_registration


@router.callback_query(F.data.in_(["dash:add:channel", "dash:add:group", "dash:add:trusted"]))
async def cb_add_start(call: CallbackQuery) -> None:
    if not logic.is_admin(call.from_user.id):
        await call.answer("Только для владельца.", show_alert=True)
        return
    target = call.data.split(":")[2]
    state.awaiting_registration[call.from_user.id] = target
    kb = InlineKeyboardMarkup(inline_keyboard=[[
        InlineKeyboardButton(text="Отмена", callback_data="dash:add:cancel")
    ]])
    if target == "trusted":
        prompt = (
            "Пришли мне @username, ID пользователя, перешли его сообщение или ответь "
            "этим сообщением на его сообщение (Reply) — добавлю в доверенные."
        )
    else:
        noun = "канал" if target == "channel" else "группу"
        prompt = (
            f"Пришли мне сюда @username или ID чата — либо просто перешли любое сообщение "
            f"из этого {noun}а. Бот должен уже быть там админом."
        )
    await call.message.edit_text(prompt, reply_markup=kb)
    await call.answer()


@router.callback_query(F.data == "dash:add:cancel")
async def cb_add_cancel(call: CallbackQuery) -> None:
    state.awaiting_registration.pop(call.from_user.id, None)
    text, kb = await render_root()
    await _safe_edit(call, text, kb)
    await call.answer("Отменил.")


@router.message(F.chat.type == ChatType.PRIVATE, _is_awaiting_registration)
async def handle_registration_reply(message: Message, bot: Bot) -> None:
    user_id = message.from_user.id
    target = state.awaiting_registration.pop(user_id, None)
    if target is None:
        return

    if target == "trusted":
        await _handle_add_trusted(message, bot)
        return

    chat_type = target
    if message.forward_from_chat:
        identifier: int | str = message.forward_from_chat.id
    else:
        raw = (message.text or "").strip()
        if raw.lower() in ("отмена", "cancel", "/cancel"):
            await message.answer("Отменил.")
            return
        if not raw:
            state.awaiting_registration[user_id] = chat_type
            await message.answer("Пусто — пришли @username, ID или перешли сообщение из чата.")
            return
        if raw.lstrip("-").isdigit():
            identifier = int(raw)
        else:
            identifier = raw if raw.startswith("@") else f"@{raw}"

    try:
        chat = await bot.get_chat(identifier)
    except TelegramBadRequest:
        state.awaiting_registration[user_id] = chat_type
        await message.answer("Не нашёл такой чат. Проверь @username/ID и пришли ещё раз (или «отмена»).")
        return

    me = await bot.get_me()
    try:
        member = await bot.get_chat_member(chat.id, me.id)
    except TelegramBadRequest:
        await message.answer("Не смог проверить права бота там — убедись, что бот вообще добавлен в чат.")
        return

    if member.status not in ("administrator", "creator"):
        await message.answer("Бот там не админ. Выдай ему права администратора и пришли ссылку/username ещё раз.")
        return

    real_type = "channel" if chat.type == "channel" else ("group" if chat.type in ("group", "supergroup") else "other")
    await db.upsert_chat_info(chat.id, real_type, chat.title or "", chat.username or "")

    name = f"@{chat.username}" if chat.username else (chat.title or str(chat.id))
    mismatch = ""
    if real_type != chat_type and real_type in ("group", "channel"):
        mismatch = f" (это оказалась {'группа' if real_type == 'group' else 'канал'}, но я всё равно подключил)"
    await message.answer(f"Подключил «{name}»{mismatch}.")

    text, kb = await render_category(real_type if real_type in ("group", "channel") else chat_type, 0)
    await message.answer(text, reply_markup=kb)


async def _handle_add_trusted(message: Message, bot: Bot) -> None:
    user_id_val: int | None = None
    username = ""
    first_name = ""

    if message.forward_from:
        u = message.forward_from
        user_id_val, username, first_name = u.id, u.username or "", u.first_name or ""
    elif message.reply_to_message and message.reply_to_message.from_user:
        u = message.reply_to_message.from_user
        user_id_val, username, first_name = u.id, u.username or "", u.first_name or ""
    else:
        raw = (message.text or "").strip()
        if raw.lower() in ("отмена", "cancel", "/cancel"):
            await message.answer("Отменил.")
            return
        if not raw:
            state.awaiting_registration[message.from_user.id] = "trusted"
            await message.answer("Пусто — пришли @username, ID, перешли сообщение или ответь на него.")
            return
        identifier: int | str = int(raw) if raw.lstrip("-").isdigit() else (raw if raw.startswith("@") else f"@{raw}")
        try:
            chat = await bot.get_chat(identifier)
        except TelegramBadRequest:
            state.awaiting_registration[message.from_user.id] = "trusted"
            await message.answer("Не нашёл такого пользователя. Проверь @username/ID и пришли ещё раз (или «отмена»).")
            return
        user_id_val, username, first_name = chat.id, chat.username or "", chat.first_name or ""

    await db.add_trusted(user_id_val, username, first_name)
    await db.add_audit(0, message.from_user.id, "trust_add", f"user={user_id_val}")
    label = f"@{username}" if username else (first_name or str(user_id_val))
    await message.answer(f"{label} добавлен(а) в доверенные — капча ему больше не покажется.")

    text, kb = await render_trusted(0)
    await message.answer(text, reply_markup=kb)
