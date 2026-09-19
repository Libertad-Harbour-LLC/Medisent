"""Конфигурация из переменных окружения. Ключей в коде нет.

Бот умеет жить в двух режимах:
  • постоянный процесс (локально, Railway, любой VPS) — long polling + SQLite;
  • serverless (Vercel) — вебхук + Redis, потому что там нет ни вечного
    процесса, ни диска, переживающего вызов функции.
Режим определяется по окружению, руками переключать ничего не нужно.
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field


def _env(name: str, default: str | None = None, *, required: bool = False) -> str:
    value = os.getenv(name, default)
    if required and not value:
        raise RuntimeError(f"Не задана обязательная переменная окружения {name}")
    return value or ""


def _env_int(name: str, default: int) -> int:
    raw = os.getenv(name)
    return int(raw) if raw else default


def _env_ids(name: str) -> frozenset[int]:
    raw = os.getenv(name, "").replace(";", ",")
    return frozenset(int(part) for part in raw.split(",") if part.strip())


def _first_env(*names: str) -> str:
    """Первое непустое значение из нескольких имён.

    Upstash в маркетплейсе Vercel кладёт одни и те же данные под разными
    именами в зависимости от того, как подключили интеграцию.
    """
    for name in names:
        value = os.getenv(name)
        if value:
            return value
    return ""


def _public_url() -> str:
    explicit = os.getenv("PUBLIC_URL")
    if explicit:
        return explicit.rstrip("/")

    # На Vercel домен приходит сам: сначала стабильный, потом адрес деплоя.
    for name in ("VERCEL_PROJECT_PRODUCTION_URL", "VERCEL_URL"):
        host = os.getenv(name)
        if host:
            return f"https://{host.rstrip('/')}"

    return ""


@dataclass(frozen=True, slots=True)
class Config:
    telegram_token: str
    allowed_user_ids: frozenset[int]

    kie_api_key: str
    kie_base_url: str
    chat_model: str
    image_model: str
    video_model: str

    public_url: str
    callback_secret: str
    telegram_webhook_secret: str
    web_host: str
    web_port: int

    database_path: str
    redis_url: str
    redis_token: str

    log_level: str
    request_timeout: float
    max_reply_tokens: int
    user_rate_limit_per_minute: int

    restricted: bool = field(init=False)
    serverless: bool = field(init=False)

    def __post_init__(self) -> None:
        object.__setattr__(self, "restricted", bool(self.allowed_user_ids))
        # На Vercel нет постоянного процесса: работаем вебхуком и Redis.
        object.__setattr__(self, "serverless", bool(os.getenv("VERCEL")))

    @property
    def callback_path(self) -> str:
        return f"/callback/kie/{self.callback_secret}"

    @property
    def callback_url(self) -> str:
        """Адрес, который уходит провайдеру в поле callBackUrl."""
        return f"{self.public_url}{self.callback_path}"

    @property
    def telegram_webhook_path(self) -> str:
        return f"/telegram/{self.telegram_webhook_secret}"

    @property
    def telegram_webhook_url(self) -> str:
        return f"{self.public_url}{self.telegram_webhook_path}"

    @property
    def callbacks_enabled(self) -> bool:
        return bool(self.public_url and self.callback_secret)

    @property
    def redis_enabled(self) -> bool:
        return bool(self.redis_url and self.redis_token)


def load_config() -> Config:
    return Config(
        telegram_token=_env("TELEGRAM_BOT_TOKEN", required=True),
        allowed_user_ids=_env_ids("TELEGRAM_ALLOWED_USER_IDS"),
        kie_api_key=_env("KIE_API_KEY", required=True),
        kie_base_url=_env("KIE_BASE_URL", "https://api.kie.ai"),
        chat_model=_env("KIE_CHAT_MODEL", "claude-sonnet-5"),
        image_model=_env("KIE_IMAGE_MODEL", "gpt-image-2-5-flare-text-to-image"),
        video_model=_env("KIE_VIDEO_MODEL", "kling-3.0/video"),
        public_url=_public_url(),
        callback_secret=_env("CALLBACK_SECRET"),
        telegram_webhook_secret=_env("TELEGRAM_WEBHOOK_SECRET"),
        web_host=_env("WEB_HOST", "0.0.0.0"),
        web_port=_env_int("PORT", 8080),
        database_path=_env("DATABASE_PATH", "data/tasks.sqlite3"),
        redis_url=_first_env("KV_REST_API_URL", "UPSTASH_REDIS_REST_URL", "REDIS_REST_URL"),
        redis_token=_first_env(
            "KV_REST_API_TOKEN", "UPSTASH_REDIS_REST_TOKEN", "REDIS_REST_TOKEN"
        ),
        log_level=_env("LOG_LEVEL", "INFO"),
        request_timeout=float(_env("REQUEST_TIMEOUT", "60")),
        max_reply_tokens=_env_int("MAX_REPLY_TOKENS", 4096),
        user_rate_limit_per_minute=_env_int("USER_RATE_LIMIT_PER_MINUTE", 6),
    )
