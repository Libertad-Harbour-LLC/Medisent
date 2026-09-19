"""Доставка готового результата в чат.

Два способа, и выбор между ними — не вкусовщина:

  по ссылке   — Telegram сам скачивает файл. Нам не нужны ни память, ни время,
                что критично в serverless. Потолок: 5 МБ фото, 20 МБ остальное.
  загрузкой   — мы качаем файл и отдаём его байтами. Потолок 50 МБ, но нужно
                держать файл в памяти и уложиться в лимит длительности функции.

Ссылки провайдера временные, так что в обоих случаях всё происходит сразу.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass

import httpx
from aiogram import Bot
from aiogram.types import BufferedInputFile, URLInputFile

from .. import texts

log = logging.getLogger(__name__)

# Лимиты Telegram на отправку ботом.
UPLOAD_LIMIT = 50 * 1024 * 1024
URL_PHOTO_LIMIT = 5 * 1024 * 1024
URL_FILE_LIMIT = 20 * 1024 * 1024

DOWNLOAD_TIMEOUT = httpx.Timeout(180.0, connect=15.0)
HEAD_TIMEOUT = httpx.Timeout(15.0, connect=5.0)


@dataclass(slots=True)
class Downloaded:
    content: bytes
    filename: str

    @property
    def size_mb(self) -> float:
        return len(self.content) / 1024 / 1024


class DeliveryService:
    def __init__(self, bot: Bot, *, prefer_url: bool = False) -> None:
        self._bot = bot
        # В serverless качать файл через себя — лишняя память и секунды.
        self._prefer_url = prefer_url

    async def send_image(self, chat_id: int, url: str, caption: str) -> None:
        if self._prefer_url and await _fits(url, URL_PHOTO_LIMIT):
            await self._bot.send_photo(chat_id, URLInputFile(url), caption=caption)
            return

        file = await _download(url, default_name="image.png")
        await self._bot.send_photo(
            chat_id,
            BufferedInputFile(file.content, filename=file.filename),
            caption=caption,
        )

    async def send_video(self, chat_id: int, url: str, caption: str) -> None:
        if self._prefer_url:
            size = await _size_of(url)
            if size is not None and size <= URL_FILE_LIMIT:
                await self._bot.send_video(
                    chat_id, URLInputFile(url), caption=caption, supports_streaming=True
                )
                return
            if size is not None and size > UPLOAD_LIMIT:
                await self._too_big(chat_id, size, url)
                return
            # Размер неизвестен или между лимитами — пробуем загрузкой.

        file = await _download(url, default_name="video.mp4")
        if len(file.content) > UPLOAD_LIMIT:
            await self._too_big(chat_id, len(file.content), url)
            return

        await self._bot.send_video(
            chat_id,
            BufferedInputFile(file.content, filename=file.filename),
            caption=caption,
            supports_streaming=True,
        )

    async def _too_big(self, chat_id: int, size_bytes: int, url: str) -> None:
        size_mb = size_bytes / 1024 / 1024
        log.warning("видео %.1f МБ не помещается в лимит Telegram", size_mb)
        await self._bot.send_message(
            chat_id, texts.VIDEO_TOO_BIG.format(size=f"{size_mb:.0f}", url=url)
        )


async def _size_of(url: str) -> int | None:
    """Размер файла по заголовку, без скачивания. None — сервер не сказал."""
    try:
        async with httpx.AsyncClient(timeout=HEAD_TIMEOUT, follow_redirects=True) as client:
            response = await client.head(url)
            length = response.headers.get("content-length")
            return int(length) if length else None
    except (httpx.HTTPError, ValueError) as exc:
        log.info("не удалось узнать размер %s: %s", url, exc)
        return None


async def _fits(url: str, limit: int) -> bool:
    size = await _size_of(url)
    # Неизвестный размер считаем подходящим: Telegram сам откажется, и тогда
    # сработает запасной путь через загрузку.
    return size is None or size <= limit


async def _download(url: str, *, default_name: str) -> Downloaded:
    async with httpx.AsyncClient(timeout=DOWNLOAD_TIMEOUT, follow_redirects=True) as client:
        response = await client.get(url)
        response.raise_for_status()
        content = response.content

    name = url.rsplit("/", 1)[-1].split("?", 1)[0] or default_name
    if "." not in name:
        name = default_name

    log.info("скачано %s (%.1f МБ)", name, len(content) / 1024 / 1024)
    return Downloaded(content=content, filename=name)
