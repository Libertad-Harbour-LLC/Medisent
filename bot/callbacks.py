"""Приём колбэков провайдера и доставка результата в чат.

Провайдер шлёт POST на callBackUrl, когда задача завершилась — успешно или нет.
Секрет в пути — единственная защита, которую даёт их API: подписи запроса в
предоставленной документации нет.
"""

from __future__ import annotations

import asyncio
import logging

from aiogram import Bot
from aiohttp import web

from . import texts
from .services.delivery import DeliveryService
from .services.jobs import parse_callback
from .storage import Storage

log = logging.getLogger(__name__)


def build_callback_handler(
    bot: Bot, storage: Storage, delivery: DeliveryService
) -> web.Handler:
    # Ссылки на фоновые задачи: без них сборщик мусора может убить доставку
    # на середине, пока провайдер уже получил свой 200.
    pending: set[asyncio.Task] = set()

    async def handler(request: web.Request) -> web.Response:
        try:
            body = await request.json()
        except Exception:  # noqa: BLE001 - тело пришло извне, доверия нет
            log.warning("колбэк с нечитаемым телом от %s", request.remote)
            return web.json_response({"code": 400, "msg": "bad json"}, status=400)

        if not isinstance(body, dict):
            return web.json_response({"code": 400, "msg": "bad body"}, status=400)

        try:
            result = parse_callback(body)
        except ValueError as exc:
            log.warning("колбэк не разобрался: %s", exc)
            return web.json_response({"code": 400, "msg": str(exc)}, status=400)

        # Отвечаем провайдеру сразу: доставка в Telegram может занять минуту,
        # а он ждёт быстрый 200 и иначе будет повторять.
        task = asyncio.create_task(_deliver(bot, storage, delivery, result))
        pending.add(task)
        task.add_done_callback(pending.discard)
        return web.json_response({"code": 200, "msg": "success"})

    return handler


async def _deliver(
    bot: Bot, storage: Storage, delivery: DeliveryService, result
) -> None:
    task = await storage.take_task(result.task_id)
    if task is None:
        # Либо чужая задача, либо повторный колбэк по уже доставленной.
        log.info("%s: задачи нет в хранилище, пропускаю", result.task_id)
        return

    if task.status_msg is not None:
        try:
            await bot.delete_message(task.chat_id, task.status_msg)
        except Exception as exc:  # noqa: BLE001 - сообщение могли удалить руками
            log.debug("не удалось убрать статус: %s", exc)

    if not result.success:
        log.warning("задача %s провалилась: %s", result.task_id, result.error)
        await bot.send_message(
            task.chat_id, texts.GENERATION_FAILED.format(reason=result.error)
        )
        return

    url = result.urls[0]
    caption = task.prompt[:1000]
    log.info(
        "задача %s готова: %s, %.3f кредита", result.task_id, task.kind, result.credits
    )

    try:
        if task.kind == "video":
            await delivery.send_video(task.chat_id, url, caption)
        else:
            await delivery.send_image(task.chat_id, url, caption)
    except Exception as exc:  # noqa: BLE001 - отдаём ссылку, раз файл не дошёл
        log.exception("не удалось доставить %s", result.task_id)
        await bot.send_message(
            task.chat_id, texts.DELIVERY_FAILED.format(reason=exc, url=url)
        )


async def health(_: web.Request) -> web.Response:
    return web.json_response({"status": "ok"})
