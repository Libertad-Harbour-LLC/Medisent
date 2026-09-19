"""Сквозной тест пути доставки: колбэк провайдера → сообщение в чат.

Telegram и скачивание файла подменены заглушками — проверяется сцепка
хранилища, разбора колбэка и выбора способа отправки, включая защиту от
повторного колбэка по одной задаче.
"""

from __future__ import annotations

import asyncio
import json
from dataclasses import dataclass, field

import pytest

from bot.callbacks import build_callback_handler
from bot.storage import Storage, Task


@dataclass
class FakeBot:
    messages: list[tuple[int, str]] = field(default_factory=list)
    deleted: list[tuple[int, int]] = field(default_factory=list)

    async def send_message(self, chat_id: int, text: str, **_: object) -> None:
        self.messages.append((chat_id, text))

    async def delete_message(self, chat_id: int, message_id: int) -> None:
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
    remote: str = "203.0.113.10"

    async def json(self) -> object:
        return self.payload


def _callback(task_id: str, *, url: str = "https://cdn.example/x.png") -> dict:
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


@pytest.fixture
async def storage(tmp_path) -> Storage:
    store = Storage(str(tmp_path / "tasks.sqlite3"))
    await store.open()
    yield store
    await store.close()


async def _settle() -> None:
    """Дожидается фоновой задачи доставки.

    Обработчик специально отвечает провайдеру до отправки файла, поэтому
    просто await-нуть его мало — надо дождаться порождённой задачи.
    """
    for _ in range(20):
        pending = [t for t in asyncio.all_tasks() if t is not asyncio.current_task()]
        if not pending:
            return
        await asyncio.wait(pending, timeout=2.0)


@pytest.mark.asyncio
async def test_image_callback_reaches_chat(storage: Storage) -> None:
    bot, delivery = FakeBot(), FakeDelivery()
    handler = build_callback_handler(bot, storage, delivery)

    await storage.add_task(
        Task("t1", "image", chat_id=777, user_id=1, status_msg=42, prompt="кот")
    )

    response = await handler(FakeRequest(_callback("t1")))
    assert response.status == 200
    await _settle()

    assert delivery.images == [(777, "https://cdn.example/x.png")]
    assert bot.deleted == [(777, 42)]  # статус «рисую» убран


@pytest.mark.asyncio
async def test_video_goes_to_video_sender(storage: Storage) -> None:
    bot, delivery = FakeBot(), FakeDelivery()
    handler = build_callback_handler(bot, storage, delivery)

    await storage.add_task(
        Task("t2", "video", chat_id=5, user_id=1, status_msg=None, prompt="кот бежит")
    )
    await handler(FakeRequest(_callback("t2", url="https://cdn.example/v.mp4")))
    await _settle()

    assert delivery.videos == [(5, "https://cdn.example/v.mp4")]
    assert not delivery.images


@pytest.mark.asyncio
async def test_duplicate_callback_delivers_once(storage: Storage) -> None:
    """Провайдер может прислать колбэк дважды — файл уходит один раз."""
    bot, delivery = FakeBot(), FakeDelivery()
    handler = build_callback_handler(bot, storage, delivery)

    await storage.add_task(
        Task("t3", "image", chat_id=9, user_id=1, status_msg=None, prompt="кот")
    )
    await handler(FakeRequest(_callback("t3")))
    await _settle()
    await handler(FakeRequest(_callback("t3")))
    await _settle()

    assert len(delivery.images) == 1


@pytest.mark.asyncio
async def test_unknown_task_is_ignored(storage: Storage) -> None:
    bot, delivery = FakeBot(), FakeDelivery()
    handler = build_callback_handler(bot, storage, delivery)

    await handler(FakeRequest(_callback("чужая-задача")))
    await _settle()

    assert not delivery.images and not bot.messages


@pytest.mark.asyncio
async def test_failed_generation_explains_reason(storage: Storage) -> None:
    bot, delivery = FakeBot(), FakeDelivery()
    handler = build_callback_handler(bot, storage, delivery)

    await storage.add_task(
        Task("t4", "video", chat_id=3, user_id=1, status_msg=None, prompt="кот")
    )
    await handler(
        FakeRequest(
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
        )
    )
    await _settle()

    assert not delivery.videos
    assert bot.messages and "generation task failed" in bot.messages[0][1].lower()


@pytest.mark.asyncio
async def test_garbage_body_returns_400(storage: Storage) -> None:
    handler = build_callback_handler(FakeBot(), storage, FakeDelivery())
    response = await handler(FakeRequest({"нет": "данных"}))
    assert response.status == 400
