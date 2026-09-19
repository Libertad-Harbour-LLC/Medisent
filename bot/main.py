"""Точка входа.

Один процесс держит две вещи одновременно:
  • long polling Telegram — боту не нужен публичный адрес, чтобы получать команды;
  • http-сервер на aiohttp — чтобы провайдер мог прислать колбэк о готовности.

На Railway порт приходит в переменной PORT, а публичный адрес — тот, что
платформа выдаёт сервису; его и кладём в PUBLIC_URL.
"""

from __future__ import annotations

import asyncio
import logging
import sys

from aiogram import Bot, Dispatcher
from aiogram.client.default import DefaultBotProperties
from aiogram.enums import ParseMode
from aiohttp import web

from .callbacks import build_callback_handler, health
from .config import Config, load_config
from .handlers import chat, common, generate
from .middlewares import AccessMiddleware, RateLimitMiddleware
from .services.claude import ClaudeService
from .services.delivery import DeliveryService
from .services.jobs import JobsService
from .services.kie import KieClient
from .storage import Storage

log = logging.getLogger("bot")


def setup_logging(level: str) -> None:
    logging.basicConfig(
        level=getattr(logging, level.upper(), logging.INFO),
        format="%(asctime)s | %(levelname)-7s | %(name)s | %(message)s",
        stream=sys.stdout,
    )
    logging.getLogger("aiogram.event").setLevel(logging.WARNING)
    logging.getLogger("httpx").setLevel(logging.WARNING)


async def run_web_app(config: Config, handler: web.Handler) -> web.AppRunner:
    app = web.Application()
    app.router.add_get("/health", health)
    if config.callbacks_enabled:
        app.router.add_post(config.callback_path, handler)

    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, config.web_host, config.web_port)
    await site.start()
    log.info("http-сервер слушает %s:%d", config.web_host, config.web_port)
    return runner


async def main() -> None:
    config = load_config()
    setup_logging(config.log_level)

    if not config.callbacks_enabled:
        log.warning(
            "PUBLIC_URL или CALLBACK_SECRET не заданы — картинки и видео будут "
            "создаваться, но результат в чат не придёт"
        )
    if not config.restricted:
        log.warning("TELEGRAM_ALLOWED_USER_IDS пуст — бот отвечает кому угодно")

    storage = Storage(config.database_path)
    await storage.open()

    kie = KieClient(
        config.kie_api_key, config.kie_base_url, timeout=config.request_timeout
    )
    bot = Bot(
        config.telegram_token,
        default=DefaultBotProperties(parse_mode=ParseMode.HTML),
    )

    claude = ClaudeService(kie, config.chat_model, config.max_reply_tokens)
    jobs_service = JobsService(
        kie,
        image_model=config.image_model,
        video_model=config.video_model,
        callback_url=config.callback_url if config.callbacks_enabled else "",
    )
    delivery = DeliveryService(bot)

    dispatcher = Dispatcher()
    dispatcher.message.middleware(AccessMiddleware(config.allowed_user_ids))
    dispatcher.message.middleware(RateLimitMiddleware(config.user_rate_limit_per_minute))
    dispatcher.include_routers(common.router, generate.router, chat.router)

    # Зависимости хендлеров — через workflow_data, без глобальных переменных.
    dispatcher.workflow_data.update(
        storage=storage,
        claude=claude,
        jobs_service=jobs_service,
        config=config,
    )

    runner = await run_web_app(config, build_callback_handler(bot, storage, delivery))

    try:
        await bot.delete_webhook(drop_pending_updates=True)
        log.info("запускаю long polling")
        await dispatcher.start_polling(bot)
    finally:
        log.info("останавливаюсь")
        await runner.cleanup()
        await kie.aclose()
        await bot.session.close()
        await storage.close()


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except (KeyboardInterrupt, SystemExit):
        pass
