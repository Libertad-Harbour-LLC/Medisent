"""Общий обработчик ошибок диспетчера.

Хендлеры ловят только то, на что отвечают по-разному (``GeminiError`` →
«не распознано», ``MailError`` → «письмо не ушло»). Всё остальное — сбой
базы, ``TelegramBadRequest`` на длинном сообщении, ``KeyError`` в payload —
раньше уходило в лог aiogram, а владелец не видел ничего и слал запрос
заново. Теперь любое необработанное исключение отвечает в исходный чат.
"""

from __future__ import annotations

import logging

from aiogram import Bot, Router
from aiogram.types import ErrorEvent

from bot import texts

logger = logging.getLogger(__name__)
router = Router(name="errors")


@router.errors()
async def on_error(event: ErrorEvent, bot: Bot) -> bool:
    update = event.update
    chat_id: int | None = None
    if update.message is not None:
        chat_id = update.message.chat.id
    elif update.callback_query is not None and update.callback_query.message is not None:
        chat_id = update.callback_query.message.chat.id

    logger.error(
        "Необработанная ошибка в update %s: %s",
        update.update_id,
        event.exception,
        exc_info=event.exception,
    )
    if chat_id is not None:
        try:
            await bot.send_message(chat_id, texts.ERROR_GENERIC)
        except Exception:
            logger.exception("Не удалось сообщить владельцу об ошибке")
    return True
