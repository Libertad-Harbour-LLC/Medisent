"""Маршрут доставки, упакованный в адрес колбэка.

Провайдеру мы сами говорим, куда стучаться по готовности. Значит, «кому
отправить результат» можно положить прямо в этот адрес — и никакого хранилища
между вызовами не нужно: маршрут возвращается вместе с результатом.

Payload подписан HMAC на том же секрете, что и путь колбэка: без подписи любой,
кто увидел адрес, мог бы прислать себе чужую картинку или спамить в чужой чат.
"""

from __future__ import annotations

import base64
import hmac
import json
from dataclasses import asdict, dataclass
from hashlib import sha256

SIGNATURE_BYTES = 16


class BadToken(ValueError):
    """Токен подделан, испорчен или собран другим секретом."""


@dataclass(slots=True)
class Route:
    chat_id: int
    status_msg: int | None
    kind: str
    prompt: str


def _b64encode(raw: bytes) -> str:
    return base64.urlsafe_b64encode(raw).decode().rstrip("=")


def _b64decode(value: str) -> bytes:
    padding = "=" * (-len(value) % 4)
    return base64.urlsafe_b64decode(value + padding)


def _sign(payload: bytes, secret: str) -> str:
    digest = hmac.new(secret.encode(), payload, sha256).digest()
    return _b64encode(digest[:SIGNATURE_BYTES])


def encode(route: Route, secret: str) -> str:
    """Собирает подписанный токен для подстановки в путь колбэка."""
    data = asdict(route)
    # Подпись коротких подписей ради: длинный промпт в адресе ни к чему,
    # он нужен только как подпись к файлу.
    data["prompt"] = route.prompt[:200]

    payload = json.dumps(data, ensure_ascii=False, separators=(",", ":")).encode()
    return f"{_b64encode(payload)}.{_sign(payload, secret)}"


def decode(token: str, secret: str) -> Route:
    """Проверяет подпись и достаёт маршрут. Бросает BadToken."""
    body, _, signature = token.partition(".")
    if not body or not signature:
        raise BadToken("токен без подписи")

    try:
        payload = _b64decode(body)
    except (ValueError, TypeError) as exc:
        raise BadToken(f"токен не декодируется: {exc}") from exc

    if not hmac.compare_digest(_sign(payload, secret), signature):
        raise BadToken("подпись не совпала")

    try:
        data = json.loads(payload)
        return Route(
            chat_id=int(data["chat_id"]),
            status_msg=data.get("status_msg"),
            kind=str(data.get("kind") or "image"),
            prompt=str(data.get("prompt") or ""),
        )
    except (json.JSONDecodeError, KeyError, TypeError, ValueError) as exc:
        raise BadToken(f"битое содержимое: {exc}") from exc
