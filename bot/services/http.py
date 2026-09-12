"""Единая точка выхода наружу.

Жёсткое правило проекта: каждый внешний вызов идёт с таймаутом, ретраем и
записью стоимости в ``api_calls``. Обойти его, дёрнув httpx напрямую, можно —
но тогда вызов не попадёт ни в лог заявки, ни в счётчик бюджета, поэтому все
сервисы ходят только через ``ApiClient``.

Ретраятся: 429, 500, 502, 503, 504, таймауты и обрывы соединения.
Не ретраятся: 4xx кроме 429 — это наша ошибка, повтор её не исправит.
"""

from __future__ import annotations

import asyncio
import logging
import random
import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from decimal import Decimal
from typing import Any

import httpx

from bot.config import get_settings
from bot.logging_setup import log_extra
from bot.services import budget

logger = logging.getLogger(__name__)

RETRY_STATUSES = frozenset({429, 500, 502, 503, 504})
RETRY_EXCEPTIONS = (
    httpx.TimeoutException,
    httpx.ConnectError,
    httpx.ReadError,
    httpx.RemoteProtocolError,
)
MAX_BACKOFF_SECONDS = 30.0


@dataclass(slots=True)
class Usage:
    """Что вызов стоил, когда цена известна только по ответу (токены модели)."""

    tokens_in: int | None = None
    tokens_out: int | None = None
    cached_tokens: int | None = None
    cost_usd: Decimal | None = None


# Считает Usage по разобранному JSON ответа. Вызывается один раз на удачный
# вызов; ошибка внутри не роняет вызов, а лишь оставляет строку без цены.
PriceFn = Callable[[Any], Usage]


@dataclass(slots=True)
class CallResult:
    """Итог вызова. Исключения наружу не летят — сервисы разбирают поля."""

    ok: bool
    status_code: int | None = None
    json: Any = None
    text: str = ""
    error: str | None = None
    duration_ms: int = 0
    attempts: int = 1
    headers: dict[str, str] = field(default_factory=dict)
    budget_exceeded: bool = False

    @property
    def timed_out(self) -> bool:
        return self.error is not None and "таймаут" in self.error


# Куда писать строку расхода. По умолчанию — в базу; тесты подменяют.
MeterFn = Callable[..., Awaitable[None]]
_meter: MeterFn | None = None
# Фоновые записи в api_calls: учёт не стоит в критическом пути вызова, но
# при остановке бота дописывается до конца (``flush_meter``).
_pending: set[asyncio.Task[None]] = set()


def set_meter(fn: MeterFn | None) -> None:
    """Подменить приёмник учёта расходов (используется в тестах)."""
    global _meter
    _meter = fn


async def flush_meter() -> None:
    """Дождаться фоновых записей учёта. Вызывается при остановке."""
    if _pending:
        await asyncio.gather(*_pending, return_exceptions=True)


async def _record(
    *,
    service: str,
    operation: str | None,
    request_id: int | None,
    tokens_in: int | None,
    tokens_out: int | None,
    cost_usd: Decimal | None,
    status: str,
    duration_ms: int,
    cached_tokens: int | None = None,
) -> None:
    """Единственный счётчик расходов.

    Сначала — потолок на заявку в памяти (он должен вырасти до следующего
    вызова, а не после записи в базу), потом строка в ``api_calls``. Сбой
    учёта не должен ронять основную работу.
    """
    if status == "ok":
        budget.record(request_id, cost_usd)

    if _meter is not None:
        await _meter(
            service=service,
            operation=operation,
            request_id=request_id,
            tokens_in=tokens_in,
            tokens_out=tokens_out,
            cost_usd=cost_usd,
            status=status,
            duration_ms=duration_ms,
            cached_tokens=cached_tokens,
        )
        return

    task = asyncio.create_task(
        _write_api_call(
            service=service,
            operation=operation,
            request_id=request_id,
            tokens_in=tokens_in,
            tokens_out=tokens_out,
            cost_usd=cost_usd,
            status=status,
            duration_ms=duration_ms,
            cached_tokens=cached_tokens,
        )
    )
    _pending.add(task)
    task.add_done_callback(_pending.discard)


async def _write_api_call(**fields: Any) -> None:
    """Строка в api_calls своей короткой сессией, вне запроса, который её породил."""
    try:
        from bot.db import repo
        from bot.db.session import session_scope

        async with session_scope() as session:
            await repo.record_api_call(session, **fields)
    except Exception:
        logger.exception("Не удалось записать расход по %s", fields.get("service"))


def _backoff_seconds(attempt: int, retry_after: str | None) -> float:
    """Экспонента с джиттером; заголовок Retry-After имеет приоритет."""
    if retry_after:
        try:
            return min(float(retry_after), MAX_BACKOFF_SECONDS)
        except ValueError:
            pass
    return min(2.0**attempt + random.uniform(0, 0.5), MAX_BACKOFF_SECONDS)  # noqa: S311


class ApiClient:
    """Обёртка над httpx.AsyncClient под один внешний сервис."""

    def __init__(
        self,
        service: str,
        *,
        base_url: str = "",
        headers: dict[str, str] | None = None,
        timeout_connect: float | None = None,
        timeout_read: float | None = None,
        max_retries: int | None = None,
    ) -> None:
        settings = get_settings()
        self.service = service
        self.max_retries = max_retries if max_retries is not None else settings.http_max_retries
        self._client = httpx.AsyncClient(
            base_url=base_url,
            headers=headers or {},
            timeout=httpx.Timeout(
                connect=timeout_connect or settings.http_timeout_connect,
                read=timeout_read or settings.http_timeout_read,
                write=timeout_read or settings.http_timeout_read,
                pool=timeout_connect or settings.http_timeout_connect,
            ),
            follow_redirects=True,
            # verify не трогаем: отключать проверку сертификата запрещено.
        )

    async def aclose(self) -> None:
        await self._client.aclose()

    async def __aenter__(self) -> ApiClient:
        return self

    async def __aexit__(self, *exc: object) -> None:
        await self.aclose()

    async def request(
        self,
        method: str,
        url: str,
        *,
        operation: str | None = None,
        request_id: int | None = None,
        cost_usd: Decimal | None = None,
        tokens_in: int | None = None,
        tokens_out: int | None = None,
        expect_json: bool = True,
        retries: int | None = None,
        redact_body: bool = False,
        price: PriceFn | None = None,
        **kwargs: Any,
    ) -> CallResult:
        """Один вызов внешнего сервиса.

        ``retries=0`` — для неидемпотентных операций: повторить отправку
        письма после таймаута значит отправить его дважды.

        ``redact_body=True`` — не класть тело ответа в ``CallResult`` и не
        писать его в лог. Нужно там, где в ответе приходит секрет: обновление
        OAuth-токена возвращает access_token и refresh_token.

        ``price`` — для сервисов, у которых цена известна только по ответу
        (токены модели): считает ``Usage`` по разобранному JSON, и вызов
        пишется в ``api_calls`` одной строкой с ценой. Такой вызов тоже
        платный: потолок на заявку проверяет его по уже потраченному.
        """
        started = time.monotonic()
        max_retries = self.max_retries if retries is None else retries

        # Потолок на заявку проверяется до вызова, а не после: смысл в том,
        # чтобы деньги не ушли, а не в том, чтобы узнать об этом первым.
        allowed = (
            await budget.allow(request_id, cost_usd)
            if cost_usd
            else (await budget.allow_unpriced(request_id) if price is not None else True)
        )
        if not allowed:
            logger.warning(
                "%s %s не отправлен: исчерпан потолок на заявку",
                self.service,
                operation or url,
                extra=log_extra(request_id),
            )
            return CallResult(
                ok=False,
                error="исчерпан потолок расходов на заявку",
                budget_exceeded=True,
            )
        extra = log_extra(request_id)
        last_error: str | None = None
        status_code: int | None = None
        attempt = 0

        while attempt <= max_retries:
            attempt += 1
            try:
                response = await self._client.request(method, url, **kwargs)
                status_code = response.status_code

                if status_code in RETRY_STATUSES and attempt <= max_retries:
                    delay = _backoff_seconds(attempt, response.headers.get("Retry-After"))
                    logger.warning(
                        "%s %s вернул %s, повтор через %.1f с (попытка %s из %s)",
                        self.service,
                        operation or url,
                        status_code,
                        delay,
                        attempt,
                        max_retries + 1,
                        extra=extra,
                    )
                    await asyncio.sleep(delay)
                    continue

                response.raise_for_status()

                payload: Any = None
                if expect_json:
                    try:
                        payload = response.json()
                    except ValueError:
                        last_error = "ответ не является JSON"
                        logger.error(
                            "%s %s: ответ не JSON, первые 200 символов: %s",
                            self.service,
                            operation or url,
                            response.text[:200],
                            extra=extra,
                        )
                        break

                duration = int((time.monotonic() - started) * 1000)
                logger.info(
                    "%s %s: %s за %s мс (попыток %s)",
                    self.service,
                    operation or url,
                    status_code,
                    duration,
                    attempt,
                    extra=extra,
                )
                usage = Usage(tokens_in=tokens_in, tokens_out=tokens_out, cost_usd=cost_usd)
                if price is not None:
                    try:
                        usage = price(payload)
                    except Exception:
                        logger.exception(
                            "%s %s: не удалось посчитать стоимость по ответу",
                            self.service,
                            operation or url,
                            extra=extra,
                        )
                await _record(
                    service=self.service,
                    operation=operation,
                    request_id=request_id,
                    tokens_in=usage.tokens_in,
                    tokens_out=usage.tokens_out,
                    cost_usd=usage.cost_usd,
                    status="ok",
                    duration_ms=duration,
                    cached_tokens=usage.cached_tokens,
                )
                return CallResult(
                    ok=True,
                    status_code=status_code,
                    json=payload,
                    text=response.text,
                    duration_ms=duration,
                    attempts=attempt,
                    headers=dict(response.headers),
                )

            except httpx.HTTPStatusError as exc:
                # 4xx кроме 429: наша ошибка, повторять бессмысленно.
                status_code = exc.response.status_code
                body = "[тело скрыто]" if redact_body else exc.response.text[:200]
                last_error = f"HTTP {status_code}: {body}"
                logger.error("%s %s: %s", self.service, operation or url, last_error, extra=extra)
                break

            except RETRY_EXCEPTIONS as exc:
                kind = "таймаут" if isinstance(exc, httpx.TimeoutException) else "обрыв соединения"
                last_error = f"{kind}: {exc}"
                if attempt > max_retries:
                    logger.error(
                        "%s %s: %s, попытки исчерпаны",
                        self.service,
                        operation or url,
                        last_error,
                        extra=extra,
                    )
                    break
                delay = _backoff_seconds(attempt, None)
                logger.warning(
                    "%s %s: %s, повтор через %.1f с (попытка %s из %s)",
                    self.service,
                    operation or url,
                    last_error,
                    delay,
                    attempt,
                    max_retries + 1,
                    extra=extra,
                )
                await asyncio.sleep(delay)

            except Exception as exc:
                last_error = f"неожиданная ошибка: {exc}"
                logger.exception("%s %s упал", self.service, operation or url, extra=extra)
                break

        duration = int((time.monotonic() - started) * 1000)
        await _record(
            service=self.service,
            operation=operation,
            request_id=request_id,
            tokens_in=tokens_in,
            tokens_out=tokens_out,
            cost_usd=cost_usd,
            status="error",
            duration_ms=duration,
        )
        return CallResult(
            ok=False,
            status_code=status_code,
            error=last_error or "неизвестная ошибка",
            duration_ms=duration,
            attempts=attempt,
        )

    async def get(self, url: str, **kwargs: Any) -> CallResult:
        return await self.request("GET", url, **kwargs)

    async def post(self, url: str, **kwargs: Any) -> CallResult:
        return await self.request("POST", url, **kwargs)
