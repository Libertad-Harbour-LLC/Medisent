"""Конфигурация из переменных окружения. Ключей в коде нет."""

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
    web_host: str
    web_port: int

    database_path: str
    log_level: str
    request_timeout: float
    max_reply_tokens: int
    user_rate_limit_per_minute: int

    # Пустой allowed_user_ids означает «бот открыт всем».
    restricted: bool = field(init=False)

    def __post_init__(self) -> None:
        object.__setattr__(self, "restricted", bool(self.allowed_user_ids))

    @property
    def callback_path(self) -> str:
        return f"/callback/kie/{self.callback_secret}"

    @property
    def callback_url(self) -> str:
        """URL, который уходит провайдеру в поле callBackUrl."""
        return f"{self.public_url.rstrip('/')}{self.callback_path}"

    @property
    def callbacks_enabled(self) -> bool:
        return bool(self.public_url and self.callback_secret)


def load_config() -> Config:
    return Config(
        telegram_token=_env("TELEGRAM_BOT_TOKEN", required=True),
        allowed_user_ids=_env_ids("TELEGRAM_ALLOWED_USER_IDS"),
        kie_api_key=_env("KIE_API_KEY", required=True),
        kie_base_url=_env("KIE_BASE_URL", "https://api.kie.ai"),
        chat_model=_env("KIE_CHAT_MODEL", "claude-sonnet-5"),
        image_model=_env("KIE_IMAGE_MODEL", "gpt-image-2-5-flare-text-to-image"),
        video_model=_env("KIE_VIDEO_MODEL", "kling-3.0/video"),
        public_url=_env("PUBLIC_URL"),
        callback_secret=_env("CALLBACK_SECRET"),
        web_host=_env("WEB_HOST", "0.0.0.0"),
        web_port=_env_int("PORT", 8080),
        database_path=_env("DATABASE_PATH", "data/tasks.sqlite3"),
        log_level=_env("LOG_LEVEL", "INFO"),
        request_timeout=float(_env("REQUEST_TIMEOUT", "60")),
        max_reply_tokens=_env_int("MAX_REPLY_TOKENS", 4096),
        user_rate_limit_per_minute=_env_int("USER_RATE_LIMIT_PER_MINUTE", 6),
    )
