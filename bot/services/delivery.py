"""Доставка готового результата в чат.

Ссылки провайдера временные (tempfile.aiquickdraw.com), поэтому файл надо
скачать и отдать в Telegram сразу, а не хранить URL.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass

import httpx
from aiogram import Bot
from aiogram.types import BufferedInputFile

from .. import texts

log = logging.getLogger(__name__)

# Потолок Telegram на отправку файла ботом.
TELEGRAM_UPLOAD_LIMIT = 50 * 1024 * 1024
DOWNLOAD_TIMEOUT = httpx.Timeout(180.0, connect=15.0)


@dataclass(slots=True)
class Downloaded:
    content: bytes
    filename: str

    @property
    def size_mb(self) -> float:
        return len(self.content) / 1024 / 1024


class DeliveryService:
    def __init__(self, bot: Bot) -> None:
        self._bot = bot

    async def send_image(self, chat_id: int, url: str, caption: str) -> None:
        file = await _download(url, default_name="image.png")
        await self._bot.send_photo(
            chat_id,
            BufferedInputFile(file.content, filename=file.filename),
            caption=caption,
        )

    async def send_video(self, chat_id: int, url: str, caption: str) -> None:
        file = await _download(url, default_name="video.mp4")

        if len(file.content) > TELEGRAM_UPLOAD_LIMIT:
            log.warning("видео %.1f МБ не помещается в лимит Telegram", file.size_mb)
            await self._bot.send_message(
                chat_id,
                texts.VIDEO_TOO_BIG.format(size=f"{file.size_mb:.0f}", url=url),
            )
            return

        await self._bot.send_video(
            chat_id,
            BufferedInputFile(file.content, filename=file.filename),
            caption=caption,
            supports_streaming=True,
        )


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
