"""Базовый HTTP-клиент к api.kie.ai.

Главная особенность провайдера: код ошибки лежит в теле ответа, а не в HTTP-статусе.
Запрос с HTTP 200 может означать «закончились кредиты». Весь разбор — здесь,
выше по стеку наружу выходит только KieError.
"""

from __future__ import annotations

import asyncio
import logging
from typing import Any, Final

import httpx

log = logging.getLogger(__name__)

# Коды из спецификаций провайдера (одинаковые у image и video).
OK: Final = 200
UNAUTHORIZED: Final = 401
NO_CREDITS: Final = 402
NOT_FOUND: Final = 404
VALIDATION: Final = 422
RATE_LIMITED: Final = 429
SUBKEY_LIMIT: Final = 433
MAINTENANCE: Final = 455
SERVER_ERROR: Final = 500
GENERATION_FAILED: Final = 501
FEATURE_DISABLED: Final = 505

RETRYABLE_CODES: Final = frozenset({RATE_LIMITED, MAINTENANCE, SERVER_ERROR})
RETRYABLE_STATUS: Final = frozenset({408, 409, 429, 500, 502, 503, 504})


class KieError(Exception):
    """Ошибка провайдера: и транспортная, и та, что пришла в теле с HTTP 200."""

    def __init__(self, code: int, message: str, *, payload: Any = None) -> None:
        super().__init__(f"[{code}] {message}")
        self.code = code
        self.message = message
        self.payload = payload

    @property
    def retryable(self) -> bool:
        return self.code in RETRYABLE_CODES


class KieNetworkError(KieError):
    """Провайдер не ответил или оборвал соединение."""

    def __init__(self, message: str) -> None:
        super().__init__(-1, message)

    @property
    def retryable(self) -> bool:
        return True


class KieClient:
    """Тонкая обёртка над httpx с ретраями и разбором конверта ответа."""

    def __init__(
        self,
        api_key: str,
        base_url: str,
        *,
        timeout: float = 60.0,
        max_attempts: int = 3,
    ) -> None:
        self._max_attempts = max_attempts
        self._client = httpx.AsyncClient(
            base_url=base_url.rstrip("/"),
            timeout=httpx.Timeout(timeout, connect=10.0),
            headers={
                "Authorization": f"Bearer {api_key}",
                "Content-Type": "application/json",
                "Accept": "application/json",
            },
        )

    async def aclose(self) -> None:
        await self._client.aclose()

    async def post(self, path: str, payload: dict[str, Any]) -> dict[str, Any]:
        """POST с ретраями. Возвращает распарсенное тело, бросает KieError."""
        last: KieError | None = None

        for attempt in range(1, self._max_attempts + 1):
            try:
                response = await self._client.post(path, json=payload)
            except httpx.TimeoutException as exc:
                last = KieNetworkError(f"таймаут: {exc}")
            except httpx.HTTPError as exc:
                last = KieNetworkError(f"сеть: {exc}")
            else:
                if response.status_code in RETRYABLE_STATUS:
                    last = KieError(
                        response.status_code,
                        f"HTTP {response.status_code}",
                        payload=_safe_text(response),
                    )
                else:
                    return self._unwrap(response)

            log.warning(
                "kie.post %s попытка %d/%d не удалась: %s",
                path,
                attempt,
                self._max_attempts,
                last,
            )
            if attempt < self._max_attempts:
                await asyncio.sleep(2 ** (attempt - 1))

        assert last is not None
        raise last

    def _unwrap(self, response: httpx.Response) -> dict[str, Any]:
        try:
            body = response.json()
        except ValueError as exc:
            raise KieError(
                response.status_code,
                f"ответ не JSON: {exc}",
                payload=_safe_text(response),
            ) from exc

        if not isinstance(body, dict):
            raise KieError(response.status_code, "ответ не объект", payload=body)

        # Конверт с кодом внутри тела есть у /api/v1/jobs/*, но не у /claude/v1/messages.
        code = body.get("code")
        if code is None and isinstance(body.get("data"), dict):
            # У Kling код продублирован внутри data.
            code = body["data"].get("code")

        if code is not None:
            code = int(code)
            if code != OK:
                raise KieError(code, str(body.get("msg") or "без описания"), payload=body)

        if response.status_code >= 400:
            raise KieError(
                response.status_code,
                f"HTTP {response.status_code}",
                payload=_safe_text(response),
            )

        return body


def _safe_text(response: httpx.Response, limit: int = 500) -> str:
    try:
        return response.text[:limit]
    except Exception:  # noqa: BLE001 - диагностика не должна ронять обработку
        return "<тело недоступно>"
