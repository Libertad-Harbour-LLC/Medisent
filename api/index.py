"""Точка входа для Vercel: ASGI-приложение вместо вечного процесса.

Vercel вызывает это приложение на каждый HTTP-запрос. Три маршрута:

    POST /telegram/<секрет>              обновления от Telegram
    POST /callback/kie/<секрет>/<токен>  готовый результат от провайдера
    GET  /health                         проверка живости

Секреты считаются из токена бота, отдельных переменных под них нет. В токене
колбэка подписан маршрут доставки — какому чату вернуть файл, — поэтому между
вызовами функции ничего хранить не требуется.

Важное отличие от постоянного процесса: всё должно быть сделано ДО возврата
ответа. Фоновая задача здесь не переживёт конец вызова.
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
from bot.services.tokens import BadToken, decode  # noqa: E402

log = logging.getLogger("api")

# Собирается один раз на «тёплый» инстанс: пересоздавать бота на каждый запрос
# дорого, а Vercel переиспользует инстанс, пока тот жив.
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
        "public_url": config.public_url or None,
        "remembers_context": _app.storage.remembers,
    }


@app.post("/telegram/{secret}")
async def telegram_webhook(
    secret: str,
    request: Request,
    x_telegram_bot_api_secret_token: str | None = Header(default=None),
) -> Response:
    expected = _app.config.telegram_webhook_secret

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


@app.post("/callback/kie/{secret}/{token}")
async def kie_callback(secret: str, token: str, request: Request) -> dict[str, object]:
    if secret != _app.config.callback_secret:
        raise HTTPException(404, "not found")

    try:
        route = decode(token, _app.config.callback_secret)
    except BadToken as exc:
        log.warning("колбэк с негодным токеном: %s", exc)
        raise HTTPException(403, "bad token") from exc

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
    await deliver_result(_app.bot, _app.delivery, route, result)

    return {"code": 200, "msg": "success"}
