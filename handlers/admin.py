from __future__ import annotations

from aiogram import Router
from aiogram.filters import Command, CommandObject
from aiogram.types import Message

import logic

router = Router(name="admin")


@router.message(Command("captcha"))
async def cmd_captcha(message: Message, command: CommandObject) -> None:
    """/captcha on|off — включить/выключить капчу вручную для текущего чата."""
    if not logic.is_admin(message.from_user and message.from_user.id):
        return
    arg = (command.args or "").strip().lower()
    if arg not in ("on", "off"):
        await message.reply("Используй: /captcha on  или  /captcha off")
        return

    await logic.set_captcha(message.chat.id, arg == "on")
    state = "включена ✅" if arg == "on" else "выключена ❌"
    await message.reply(f"Капча на вход {state} (вручную, до следующей команды).")


@router.message(Command("captcha_auto"))
async def cmd_captcha_auto(message: Message) -> None:
    """/captcha_auto — вернуть капчу под автоматическое управление антирейдом."""
    if not logic.is_admin(message.from_user and message.from_user.id):
        return
    await logic.set_captcha_auto(message.chat.id)
    await message.reply("Ок, капчей теперь снова управляет антирейд-система автоматически.")


@router.message(Command("raid_status"))
async def cmd_raid_status(message: Message) -> None:
    """/raid_status — текущее состояние защиты в этом чате."""
    if not logic.is_admin(message.from_user and message.from_user.id):
        return
    await message.reply(await logic.status_text(message.chat.id))


@router.message(Command("raid_off"))
async def cmd_raid_off(message: Message) -> None:
    """/raid_off — досрочно снять авто-режим рейда (вернуть капчу как было до него)."""
    if not logic.is_admin(message.from_user and message.from_user.id):
        return
    was_active = await logic.turn_off_raid(message.chat.id)
    if was_active:
        await message.reply("Режим рейда снят, капча возвращена к прежнему состоянию.")
    else:
        await message.reply("Режим рейда сейчас и так не активен.")
