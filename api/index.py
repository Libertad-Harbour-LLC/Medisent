"""Точка входа для Vercel: ASGI-приложение вместо вечного процесса.

Здесь нет ни поллинга, ни своего сервера — Vercel сам вызывает это приложение
на каждый HTTP-запрос. Два маршрута, оба закрыты секретом в пути:

    POST /telegram/<секрет>      обновления от Telegram
    POST /callback/kie/<секрет>  готовый результат от провайдера
    GET  /health                 проверка живости

Важное отличие от постоянного процесса: всё должно быть сделано ДО возврата
ответа. Фоновая задача здесь не переживёт конец вызова, поэтому доставка файла
в чат происходит внутри обработчика колбэка, а не после.
"""

from __future__ import annotations

import logging
import sys
from pathlib import Path

from fastapi import FastAPI, Header, HTTPException, Request, Response

# Vercel запускает файл из каталога api/, корень проекта в путь не добавлен.
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from bot.assembly import build_app  # noqa: E402
from bot.callbacks import deliver_result  # noqa: E402
from bot.services.jobs import parse_callback  # noqa: E402

log = logging.getLogger("api")

# Собирается один раз на «тёплый» инстанс: пересоздавать бота на каждый запрос
# дорого, а Vercel переиспользует инстанс между вызовами, пока тот жив.
_app = build_app()
_started = False

app = FastAPI(docs_url=None, redoc_url=None, openapi_url=None)


async def _ensure_started() -> None:
    global _started
    if not _started:
        await _app.start()
        _started = True


@app.get("/health")
async def health() -> dict[str, object]:
    config = _app.config
    return {
        "status": "ok",
        "serverless": config.serverless,
        "callbacks": config.callbacks_enabled,
        "storage": type(_app.storage).__name__,
    }


@app.post("/telegram/{secret}")
async def telegram_webhook(
    secret: str,
    request: Request,
    x_telegram_bot_api_secret_token: str | None = Header(default=None),
) -> Response:
    expected = _app.config.telegram_webhook_secret
    if not expected:
        raise HTTPException(500, "TELEGRAM_WEBHOOK_SECRET не задан")

    # Секрет в пути — от случайных запросов, заголовок — от подделки: его
    # Telegram присылает сам, если передать secret_token при setWebhook.
    if secret != expected:
        raise HTTPException(404, "not found")
    if x_telegram_bot_api_secret_token and x_telegram_bot_api_secret_token != expected:
        raise HTTPException(403, "bad secret token")

    await _ensure_started()

    from aiogram.types import Update

    update = Update.model_validate(await request.json(), context={"bot": _app.bot})
    await _app.dispatcher.feed_update(_app.bot, update)
    return Response(status_code=200)


@app.post("/callback/kie/{secret}")
async def kie_callback(secret: str, request: Request) -> dict[str, object]:
    if not _app.config.callback_secret or secret != _app.config.callback_secret:
        raise HTTPException(404, "not found")

    try:
        body = await request.json()
    except Exception:  # noqa: BLE001 - тело пришло извне, доверия нет
        raise HTTPException(400, "bad json") from None

    if not isinstance(body, dict):
        raise HTTPException(400, "bad body")

    try:
        result = parse_callback(body)
    except ValueError as exc:
        log.warning("колбэк не разобрался: %s", exc)
        raise HTTPException(400, str(exc)) from exc

    await _ensure_started()
    # Именно await, не фоновая задача: после возврата ответа функция умрёт.
    await deliver_result(_app.bot, _app.storage, _app.delivery, result)

    return {"code": 200, "msg": "success"}
