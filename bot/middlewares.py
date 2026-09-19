"""Доступ и ограничение частоты — до того, как запрос дойдёт до платного API."""

from __future__ import annotations

import logging
import time
from collections import defaultdict, deque
from collections.abc import Awaitable, Callable
from typing import Any

from aiogram import BaseMiddleware
from aiogram.types import Message, TelegramObject

from . import texts

log = logging.getLogger(__name__)


class AccessMiddleware(BaseMiddleware):
    """Пускает только разрешённых пользователей. Пустой список — бот открыт."""

    def __init__(self, allowed: frozenset[int]) -> None:
        self._allowed = allowed

    async def __call__(
        self,
        handler: Callable[[TelegramObject, dict[str, Any]], Awaitable[Any]],
        event: TelegramObject,
        data: dict[str, Any],
    ) -> Any:
        if not self._allowed:
            return await handler(event, data)

        user = data.get("event_from_user")
        if user is None or user.id not in self._allowed:
            log.warning("отказано пользователю %s", getattr(user, "id", "?"))
            if isinstance(event, Message):
                await event.answer(texts.NOT_ALLOWED)
            return None

        return await handler(event, data)


class RateLimitMiddleware(BaseMiddleware):
    """Скользящее окно на пользователя.

    Состояние в памяти: при рестарте лимиты обнуляются, но это защита от
    случайной очереди запросов, а не от злоумышленника. Для нескольких
    инстансов сюда нужен Redis.
    """

    def __init__(self, per_minute: int) -> None:
        self._limit = per_minute
        self._hits: dict[int, deque[float]] = defaultdict(deque)

    async def __call__(
        self,
        handler: Callable[[TelegramObject, dict[str, Any]], Awaitable[Any]],
        event: TelegramObject,
        data: dict[str, Any],
    ) -> Any:
        user = data.get("event_from_user")
        if user is None or self._limit <= 0:
            return await handler(event, data)

        now = time.monotonic()
        hits = self._hits[user.id]
        while hits and now - hits[0] > 60.0:
            hits.popleft()

        if len(hits) >= self._limit:
            log.info("лимит частоты для пользователя %s", user.id)
            if isinstance(event, Message):
                await event.answer(texts.RATE_LIMITED)
            return None

        hits.append(now)
        return await handler(event, data)
