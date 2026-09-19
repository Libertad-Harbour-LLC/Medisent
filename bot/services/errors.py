"""Перевод ошибок провайдера в текст для пользователя."""

from __future__ import annotations

from .. import texts
from .kie import (
    FEATURE_DISABLED,
    GENERATION_FAILED,
    MAINTENANCE,
    NO_CREDITS,
    RATE_LIMITED,
    SERVER_ERROR,
    SUBKEY_LIMIT,
    UNAUTHORIZED,
    VALIDATION,
    KieError,
    KieNetworkError,
)


def explain(error: KieError) -> str:
    """Человеческое объяснение вместо «[402] Insufficient Quota»."""
    if isinstance(error, KieNetworkError):
        return texts.ERROR_NETWORK

    match error.code:
        case c if c == UNAUTHORIZED:
            return texts.ERROR_AUTH
        case c if c == NO_CREDITS:
            return texts.ERROR_CREDITS
        case c if c in (RATE_LIMITED, SUBKEY_LIMIT):
            return texts.ERROR_RATE_LIMIT
        case c if c == VALIDATION:
            return texts.ERROR_VALIDATION.format(detail=error.message)
        case c if c == MAINTENANCE:
            return texts.ERROR_MAINTENANCE
        case c if c in (SERVER_ERROR, GENERATION_FAILED, FEATURE_DISABLED):
            return texts.ERROR_GENERIC.format(code=error.code, detail=error.message)
        case _:
            return texts.ERROR_GENERIC.format(code=error.code, detail=error.message)
