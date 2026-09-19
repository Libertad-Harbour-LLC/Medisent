"""Сборка бота — одна для обоих режимов запуска.

Постоянный процесс и serverless отличаются только тем, кто принимает запросы.
Сам бот, роутеры и сервисы одинаковые, поэтому собираются здесь, а не
дублируются в двух точках входа.
"""

from __future__ import annotations

import logging
import sys
from dataclasses import dataclass

from aiogram import Bot, Dispatcher
from aiogram.client.default import DefaultBotProperties
from aiogram.enums import ParseMode

from .config import Config, load_config
from .handlers import chat, common, generate
from .middlewares import AccessMiddleware, RateLimitMiddleware
from .services.claude import ClaudeService
from .services.delivery import DeliveryService
from .services.jobs import JobsService
from .services.kie import KieClient
from .storage import Storage, build_storage

log = logging.getLogger(__name__)


def setup_logging(level: str) -> None:
    logging.basicConfig(
        level=getattr(logging, level.upper(), logging.INFO),
        format="%(asctime)s | %(levelname)-7s | %(name)s | %(message)s",
        stream=sys.stdout,
    )
    logging.getLogger("aiogram.event").setLevel(logging.WARNING)
    logging.getLogger("httpx").setLevel(logging.WARNING)


@dataclass(slots=True)
class App:
    config: Config
    bot: Bot
    dispatcher: Dispatcher
    storage: Storage
    delivery: DeliveryService
    kie: KieClient

    async def start(self) -> None:
        await self.storage.open()

    async def stop(self) -> None:
        await self.storage.close()
        await self.kie.aclose()
        await self.bot.session.close()


def build_app(config: Config | None = None) -> App:
    config = config or load_config()
    setup_logging(config.log_level)

    if not config.callbacks_enabled:
        log.warning(
            "PUBLIC_URL не задан — картинки и видео заказать не получится, "
            "принять результат будет некуда"
        )
    if not config.restricted:
        log.warning("TELEGRAM_ALLOWED_USER_IDS пуст — бот отвечает кому угодно")

    kie = KieClient(
        config.kie_api_key, config.kie_base_url, timeout=config.request_timeout
    )
    bot = Bot(
        config.telegram_token,
        default=DefaultBotProperties(parse_mode=ParseMode.HTML),
    )

    dispatcher = Dispatcher()
    dispatcher.message.middleware(AccessMiddleware(config.allowed_user_ids))
    dispatcher.message.middleware(RateLimitMiddleware(config.user_rate_limit_per_minute))
    dispatcher.include_routers(common.router, generate.router, chat.router)

    storage = build_storage(config)

    dispatcher.workflow_data.update(
        storage=storage,
        claude=ClaudeService(kie, config.chat_model, config.max_reply_tokens),
        jobs_service=JobsService(
            kie, image_model=config.image_model, video_model=config.video_model
        ),
        config=config,
    )

    return App(
        config=config,
        bot=bot,
        dispatcher=dispatcher,
        storage=storage,
        # В serverless отдаём Telegram ссылку вместо байтов: память и секунды
        # функции дороже, чем разница в лимитах на размер файла.
        delivery=DeliveryService(bot, prefer_url=config.serverless),
        kie=kie,
    )
