"""Конфигурация из переменных окружения.

Обязательных переменных всего две: токен бота и ключ провайдера. Всё
остальное либо имеет разумное значение по умолчанию, либо выводится.

Секреты в адресах вебхука и колбэка не задаются руками, а считаются из токена
бота. Это те же неугадываемые строки, но без двух лишних переменных: токен и
так секретный, и другого источника секретности у бота всё равно нет.
"""

from __future__ import annotations

import hmac
import os
from dataclasses import dataclass, field
from hashlib import sha256


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


def _derive(token: str, purpose: str) -> str:
    """Стабильный неугадываемый идентификатор из токена бота.

    Меняется только вместе с токеном — а если токен сменили, вебхук всё равно
    надо перерегистрировать.
    """
    return hmac.new(token.encode(), purpose.encode(), sha256).hexdigest()[:32]


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
    web_host: str
    web_port: int

    database_path: str
    log_level: str
    request_timeout: float
    max_reply_tokens: int
    user_rate_limit_per_minute: int

    restricted: bool = field(init=False)
    serverless: bool = field(init=False)
    callback_secret: str = field(init=False)
    telegram_webhook_secret: str = field(init=False)

    def __post_init__(self) -> None:
        object.__setattr__(self, "restricted", bool(self.allowed_user_ids))
        # На Vercel нет постоянного процесса: работаем вебхуком, не поллингом.
        object.__setattr__(self, "serverless", bool(os.getenv("VERCEL")))
        object.__setattr__(
            self, "callback_secret", _derive(self.telegram_token, "kie-callback")
        )
        object.__setattr__(
            self, "telegram_webhook_secret", _derive(self.telegram_token, "telegram-webhook")
        )

    @property
    def callback_base(self) -> str:
        """Префикс пути колбэка. Дальше подставляется подписанный маршрут."""
        return f"/callback/kie/{self.callback_secret}"

    def callback_url(self, token: str) -> str:
        """Адрес для конкретной задачи: в нём уже зашито, куда слать результат."""
        return f"{self.public_url}{self.callback_base}/{token}"

    @property
    def telegram_webhook_path(self) -> str:
        return f"/telegram/{self.telegram_webhook_secret}"

    @property
    def telegram_webhook_url(self) -> str:
        return f"{self.public_url}{self.telegram_webhook_path}"

    @property
    def callbacks_enabled(self) -> bool:
        return bool(self.public_url)


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
        web_host=_env("WEB_HOST", "0.0.0.0"),
        web_port=_env_int("PORT", 8080),
        database_path=_env("DATABASE_PATH", "data/tasks.sqlite3"),
        log_level=_env("LOG_LEVEL", "INFO"),
        request_timeout=float(_env("REQUEST_TIMEOUT", "60")),
        max_reply_tokens=_env_int("MAX_REPLY_TOKENS", 4096),
        user_rate_limit_per_minute=_env_int("USER_RATE_LIMIT_PER_MINUTE", 6),
    )
