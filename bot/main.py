"""Запуск постоянным процессом: локально, на Railway, на любом VPS.

Держит одновременно long polling Telegram и http-сервер для колбэков
провайдера. Для Vercel это не подходит — там точка входа api/index.py.
"""

from __future__ import annotations

import asyncio
import logging

from aiohttp import web

from .assembly import App, build_app
from .callbacks import build_callback_handler, health

log = logging.getLogger("bot")


async def run_web_app(app: App) -> web.AppRunner:
    config = app.config
    web_app = web.Application()
    web_app.router.add_get("/health", health)

    if config.callbacks_enabled:
        web_app.router.add_post(
            config.callback_path,
            build_callback_handler(app.bot, app.storage, app.delivery),
        )

    runner = web.AppRunner(web_app)
    await runner.setup()
    site = web.TCPSite(runner, config.web_host, config.web_port)
    await site.start()
    log.info("http-сервер слушает %s:%d", config.web_host, config.web_port)
    return runner


async def main() -> None:
    app = build_app()
    if app.config.serverless:
        raise RuntimeError(
            "Это запуск постоянным процессом, а окружение выглядит как Vercel. "
            "Там точка входа api/index.py."
        )

    await app.start()
    runner = await run_web_app(app)

    try:
        await app.bot.delete_webhook(drop_pending_updates=True)
        log.info("запускаю long polling")
        await app.dispatcher.start_polling(app.bot)
    finally:
        log.info("останавливаюсь")
        await runner.cleanup()
        await app.stop()


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except (KeyboardInterrupt, SystemExit):
        pass
