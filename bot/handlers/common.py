"""Команды /start, /help, /reset."""

from __future__ import annotations

from aiogram import Router
from aiogram.filters import Command, CommandStart
from aiogram.types import Message

from .. import texts
from ..storage import Storage

router = Router(name="common")


@router.message(CommandStart())
async def on_start(message: Message) -> None:
    await message.answer(texts.START)


@router.message(Command("help"))
async def on_help(message: Message) -> None:
    await message.answer(texts.HELP)


@router.message(Command("reset"))
async def on_reset(message: Message, storage: Storage) -> None:
    if message.from_user is not None:
        await storage.clear_history(message.from_user.id)
    await message.answer(texts.CONTEXT_RESET)
