from __future__ import annotations

from aiogram import F, Router
from aiogram.enums import ChatType
from aiogram.types import Message

import logic
from config import config

# Регистрируется ПОСЛЕДНИМ в bot.py: до сюда доходят только сообщения,
# которые не разобрал ни один другой роутер (т.е. не команды и не часть капчи).
router = Router(name="gatekeeper")


@router.message(F.chat.type == ChatType.PRIVATE)
async def catch_all_private(message: Message) -> None:
    if logic.is_admin(message.from_user and message.from_user.id):
        return
    await message.answer(f"Этот бот принадлежит {config.owner_contact}.")
