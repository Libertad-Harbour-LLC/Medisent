"""Приём колбэков провайдера и доставка результата в чат.

Куда отправлять результат, известно из самого адреса колбэка: маршрут подписан
и лежит в пути (см. services/tokens.py). Никакого хранилища между вызовами.

Защита от повторной доставки — сообщение со статусом («Рисую…»). Удалить его
можно ровно один раз: если Telegram отвечает, что удалять нечего, значит этот
колбэк уже отработал и файл отправлять не надо.
"""

from __future__ import annotations

import asyncio
import logging

from aiogram import Bot
from aiogram.exceptions import TelegramBadRequest
from aiohttp import web

from . import texts
from .services.delivery import DeliveryService
from .services.jobs import TaskResult, parse_callback
from .services.tokens import BadToken, Route, decode

log = logging.getLogger(__name__)


def build_callback_handler(bot: Bot, delivery: DeliveryService, secret: str) -> web.Handler:
    """Обработчик для постоянного процесса (aiohttp)."""
    # Ссылки на фоновые задачи: без них сборщик мусора может убить доставку
    # на середине, пока провайдер уже получил свой 200.
    pending: set[asyncio.Task] = set()

    async def handler(request: web.Request) -> web.Response:
        try:
            route = decode(request.match_info["token"], secret)
        except BadToken as exc:
            log.warning("колбэк с негодным токеном от %s: %s", request.remote, exc)
            return web.json_response({"code": 403, "msg": "bad token"}, status=403)

        try:
            body = await request.json()
        except Exception:  # noqa: BLE001 - тело пришло извне, доверия нет
            return web.json_response({"code": 400, "msg": "bad json"}, status=400)

        if not isinstance(body, dict):
            return web.json_response({"code": 400, "msg": "bad body"}, status=400)

        try:
            result = parse_callback(body)
        except ValueError as exc:
            log.warning("колбэк не разобрался: %s", exc)
            return web.json_response({"code": 400, "msg": str(exc)}, status=400)

        # Отвечаем провайдеру сразу: доставка может занять минуту, а он ждёт
        # быстрый 200 и иначе будет повторять.
        task = asyncio.create_task(deliver_result(bot, delivery, route, result))
        pending.add(task)
        task.add_done_callback(pending.discard)
        return web.json_response({"code": 200, "msg": "success"})

    return handler


async def deliver_result(
    bot: Bot, delivery: DeliveryService, route: Route, result: TaskResult
) -> None:
    """Убирает статус и отправляет файл пользователю.

    В постоянном процессе вызывается фоном, в serverless — прямо в обработчике:
    там фоновая задача не переживёт возврат ответа.
    """
    if not await _claim(bot, route):
        log.info("%s: статус уже убран, считаю колбэк повторным", result.task_id)
        return

    if not result.success:
        log.warning("задача %s провалилась: %s", result.task_id, result.error)
        await bot.send_message(
            route.chat_id, texts.GENERATION_FAILED.format(reason=result.error)
        )
        return

    url = result.urls[0]
    log.info(
        "задача %s готова: %s, %.3f кредита", result.task_id, route.kind, result.credits
    )

    try:
        if route.kind == "video":
            await delivery.send_video(route.chat_id, url, route.prompt)
        else:
            await delivery.send_image(route.chat_id, url, route.prompt)
    except Exception as exc:  # noqa: BLE001 - отдаём ссылку, раз файл не дошёл
        log.exception("не удалось доставить %s", result.task_id)
        await bot.send_message(
            route.chat_id, texts.DELIVERY_FAILED.format(reason=exc, url=url)
        )


async def _claim(bot: Bot, route: Route) -> bool:
    """Пытается забрать задачу, удалив сообщение со статусом.

    Удаление удаётся один раз — это и есть защита от повторного колбэка,
    причём без всякого хранилища: состояние держит сам Telegram.
    """
    if route.status_msg is None:
        # Статуса не было, дедуплицировать нечем — доставляем.
        return True

    try:
        await bot.delete_message(route.chat_id, route.status_msg)
        return True
    except TelegramBadRequest as exc:
        log.info("статус %s уже удалён: %s", route.status_msg, exc.message)
        return False
    except Exception as exc:  # noqa: BLE001 - не смогли убрать, но доставить надо
        log.warning("не удалось убрать статус: %s", exc)
        return True


async def health(_: web.Request) -> web.Response:
    return web.json_response({"status": "ok"})
