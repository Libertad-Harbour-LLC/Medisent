"""Общее для хендлеров: скачивание файла из Telegram."""

from __future__ import annotations

from aiogram import Bot

MAX_FILE_BYTES = 20 * 1024 * 1024  # предел Telegram Bot API на скачивание


async def download(bot: Bot, file_id: str) -> bytes | None:
    """Файл из Telegram целиком. ``None`` — слишком большой или пустой.

    Одна функция на приём запроса и на голосовой выбор: раньше выбор качал
    файл сам и без проверки размера, и большое голосовое там падало вместо
    вежливого отказа.
    """
    file = await bot.get_file(file_id)
    if file.file_size and file.file_size > MAX_FILE_BYTES:
        return None
    buffer = await bot.download_file(file.file_path or "")
    return buffer.read() if buffer else None
