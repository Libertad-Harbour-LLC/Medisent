"""Сквозной тест доставки: колбэк провайдера → сообщение в чат.

Telegram и скачивание подменены заглушками. Проверяется сцепка «подписанный
маршрут → разбор колбэка → выбор способа отправки», включая защиту от
повторного колбэка: она держится на том, что сообщение со статусом можно
удалить ровно один раз.
"""

from __future__ import annotations

import asyncio
import json
from dataclasses import dataclass, field

import pytest
from aiogram.exceptions import TelegramBadRequest

from bot.callbacks import build_callback_handler, deliver_result
from bot.services.jobs import parse_callback
from bot.services.tokens import Route, encode

SECRET = "секрет"


@dataclass
class FakeBot:
    messages: list[tuple[int, str]] = field(default_factory=list)
    deleted: list[tuple[int, int]] = field(default_factory=list)
    # Сообщения, которые «уже удалены»: повторное удаление должно падать.
    gone: set[int] = field(default_factory=set)

    async def send_message(self, chat_id: int, text: str, **_: object) -> None:
        self.messages.append((chat_id, text))

    async def delete_message(self, chat_id: int, message_id: int) -> None:
        if message_id in self.gone:
            raise TelegramBadRequest(
                method=None,  # type: ignore[arg-type]
                message="Bad Request: message to delete not found",
            )
        self.gone.add(message_id)
        self.deleted.append((chat_id, message_id))


@dataclass
class FakeDelivery:
    images: list[tuple[int, str]] = field(default_factory=list)
    videos: list[tuple[int, str]] = field(default_factory=list)

    async def send_image(self, chat_id: int, url: str, caption: str) -> None:
        self.images.append((chat_id, url))

    async def send_video(self, chat_id: int, url: str, caption: str) -> None:
        self.videos.append((chat_id, url))


@dataclass
class FakeRequest:
    """Минимум от aiohttp-запроса, который нужен обработчику."""

    payload: object
    token: str
    remote: str = "203.0.113.10"

    @property
    def match_info(self) -> dict[str, str]:
        return {"token": self.token}

    async def json(self) -> object:
        return self.payload


def _callback(task_id: str = "t1", *, url: str = "https://cdn.example/x.png") -> dict:
    return {
        "code": 200,
        "msg": "Playground task completed successfully.",
        "data": {
            "taskId": task_id,
            "state": "success",
            "resultJson": json.dumps({"resultUrls": [url]}),
            "creditsConsumed": 3,
        },
    }


def _token(**overrides) -> str:
    base = dict(chat_id=777, status_msg=42, kind="image", prompt="кот")
    base.update(overrides)
    return encode(Route(**base), SECRET)


async def _settle() -> None:
    """Дожидается фоновой задачи доставки.

    Обработчик отвечает провайдеру до отправки файла, поэтому просто await-нуть
    его мало.
    """
    for _ in range(20):
        pending = [t for t in asyncio.all_tasks() if t is not asyncio.current_task()]
        if not pending:
            return
        await asyncio.wait(pending, timeout=2.0)


class TestHandler:
    @pytest.mark.asyncio
    async def test_image_reaches_chat(self) -> None:
        bot, delivery = FakeBot(), FakeDelivery()
        handler = build_callback_handler(bot, delivery, SECRET)

        response = await handler(FakeRequest(_callback(), _token()))
        assert response.status == 200
        await _settle()

        assert delivery.images == [(777, "https://cdn.example/x.png")]
        assert bot.deleted == [(777, 42)]  # статус «рисую» убран

    @pytest.mark.asyncio
    async def test_forged_token_rejected(self) -> None:
        """Без этого любой, кто узнал адрес, слал бы файлы в чужие чаты."""
        bot, delivery = FakeBot(), FakeDelivery()
        handler = build_callback_handler(bot, delivery, SECRET)

        response = await handler(FakeRequest(_callback(), encode(
            Route(chat_id=999, status_msg=1, kind="image", prompt="x"), "чужой-секрет"
        )))
        await _settle()

        assert response.status == 403
        assert not delivery.images

    @pytest.mark.asyncio
    async def test_garbage_body_returns_400(self) -> None:
        handler = build_callback_handler(FakeBot(), FakeDelivery(), SECRET)
        response = await handler(FakeRequest({"нет": "данных"}, _token()))
        assert response.status == 400


class TestDelivery:
    @pytest.mark.asyncio
    async def test_video_goes_to_video_sender(self) -> None:
        bot, delivery = FakeBot(), FakeDelivery()
        route = Route(chat_id=5, status_msg=None, kind="video", prompt="кот бежит")

        await deliver_result(
            bot, delivery, route, parse_callback(_callback(url="https://cdn/v.mp4"))
        )

        assert delivery.videos == [(5, "https://cdn/v.mp4")]
        assert not delivery.images

    @pytest.mark.asyncio
    async def test_duplicate_callback_delivers_once(self) -> None:
        """Повторный колбэк ловится тем, что статус уже удалён."""
        bot, delivery = FakeBot(), FakeDelivery()
        route = Route(chat_id=9, status_msg=7, kind="image", prompt="кот")
        result = parse_callback(_callback())

        await deliver_result(bot, delivery, route, result)
        await deliver_result(bot, delivery, route, result)

        assert len(delivery.images) == 1

    @pytest.mark.asyncio
    async def test_failed_generation_explains_reason(self) -> None:
        bot, delivery = FakeBot(), FakeDelivery()
        route = Route(chat_id=3, status_msg=None, kind="video", prompt="кот")

        await deliver_result(
            bot,
            delivery,
            route,
            parse_callback(
                {
                    "code": 501,
                    "msg": "Playground task failed.",
                    "data": {
                        "taskId": "t4",
                        "state": "fail",
                        "resultJson": None,
                        "failMsg": "The generation task failed.",
                    },
                }
            ),
        )

        assert not delivery.videos
        assert bot.messages and "generation task failed" in bot.messages[0][1].lower()

    @pytest.mark.asyncio
    async def test_without_status_message_still_delivers(self) -> None:
        bot, delivery = FakeBot(), FakeDelivery()
        route = Route(chat_id=1, status_msg=None, kind="image", prompt="кот")

        await deliver_result(bot, delivery, route, parse_callback(_callback()))

        assert len(delivery.images) == 1
