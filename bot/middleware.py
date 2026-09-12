"""Мидлвари aiogram: доступ только владельцу, номер заявки в логах, антифлуд."""

from __future__ import annotations

import logging
import time
from collections.abc import Awaitable, Callable
from typing import Any

from aiogram import BaseMiddleware
from aiogram.types import CallbackQuery, Message, TelegramObject, User

from bot import texts

logger = logging.getLogger(__name__)


class OwnerOnlyMiddleware(BaseMiddleware):
    """Whitelist на одного человека.

    Многопользовательского режима у бота нет: всё, что он делает, — тратит
    деньги владельца на внешние API и пишет письма от его имени.
    """

    def __init__(self, owner_id: int) -> None:
        self.owner_id = owner_id

    async def __call__(
        self,
        handler: Callable[[TelegramObject, dict[str, Any]], Awaitable[Any]],
        event: TelegramObject,
        data: dict[str, Any],
    ) -> Any:
        user: User | None = data.get("event_from_user")
        if user is None or user.id != self.owner_id:
            if user is not None:
                logger.warning("Отказано в доступе: telegram id=%s (@%s)", user.id, user.username)
            if isinstance(event, Message):
                await event.answer(texts.ACCESS_DENIED)
            elif isinstance(event, CallbackQuery):
                await event.answer(texts.ACCESS_DENIED, show_alert=True)
            return None
        return await handler(event, data)


class ThrottleMiddleware(BaseMiddleware):
    """Простой антифлуд: не чаще одного сообщения в ``interval`` секунд.

    Владелец один, распределённый лимитер здесь избыточен — но без всякого
    ограничения случайная серия голосовых запустит четыре подбора подряд и
    сожжёт бюджет на Perplexity.
    """

    def __init__(self, interval: float = 1.0) -> None:
        self.interval = interval
        self._last: dict[int, float] = {}
        self._last_album: dict[int, str] = {}

    async def __call__(
        self,
        handler: Callable[[TelegramObject, dict[str, Any]], Awaitable[Any]],
        event: TelegramObject,
        data: dict[str, Any],
    ) -> Any:
        # Нажатие кнопки — не флуд: одобрение и так одноразовое в базе, а
        # проглоченный callback оставлял владельцу вечно крутящуюся кнопку.
        if isinstance(event, CallbackQuery):
            return await handler(event, data)

        user: User | None = data.get("event_from_user")
        if user is not None:
            # Альбом из нескольких фото приходит отдельными сообщениями за
            # миллисекунды. Это один запрос: первое фото идёт в подбор,
            # остальные кадры того же альбома отбрасываются сознательно.
            album = event.media_group_id if isinstance(event, Message) else None
            if album and self._last_album.get(user.id) == album:
                return None
            now = time.monotonic()
            previous = self._last.get(user.id, 0.0)
            if now - previous < self.interval:
                logger.debug("Пропущено сообщение по антифлуду от %s", user.id)
                return None
            self._last[user.id] = now
            if album:
                self._last_album[user.id] = album
        return await handler(event, data)
