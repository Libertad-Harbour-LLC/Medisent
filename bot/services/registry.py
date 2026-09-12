"""Проверка изделия в реестрах Росздравнадзора.

Что этот модуль делает: отвечает на вопросы «зарегистрировано ли изделие»,
«кто держатель РУ», «действует ли удостоверение».

Чего он НЕ делает и делать не будет:

* не говорит, есть ли изделие у конкретного поставщика — дилеров в реестре нет;
* не обращается ни к какой языковой модели. Проверка реестра — детерминированный
  код, и подменять её ответом модели запрещено при любых обстоятельствах;
* не выдаёт «не найдено», когда на самом деле «не смогли проверить».

Реестра два, и проверяются оба: ``misearch`` (до 01.03.2025) и ``elk``
(после 01.03.2025). Firecrawl к elk не применяется — там свой JSON-gateway.
"""

from __future__ import annotations

import asyncio
import datetime as dt
import hashlib
import logging
from collections.abc import Sequence
from dataclasses import dataclass, field
from typing import Any

from bot.config import get_settings
from bot.db.models import RegistryState
from bot.logging_setup import log_extra
from bot.services import registry_endpoints as endpoints
from bot.services.http import ApiClient
from bot.services.registry_endpoints import RegistryRecord

logger = logging.getLogger(__name__)

# Один опрос одного реестра: что нашли и что пошло не так.
Outcome = tuple[list[RegistryRecord], str | None]


def derive_state(outcomes: Sequence[Outcome]) -> str:
    """Правило found / not_found / unavailable. Единственное место, где оно живёт.

    * хоть одна запись — ``found``;
    * записей нет, но хоть один источник не ответил или не разобрался —
      ``unavailable``: мы не смогли проверить, а не «не нашли»;
    * все источники ответили внятно и все сказали «нет» — ``not_found``.

    Правило одно для двух реестров РУ и для писем ``unrega``: подменять
    «не смогли проверить» на «ничего нет» запрещено везде одинаково.
    """
    if not outcomes:
        # Ни одного источника не опросили — значит, не проверяли.
        return RegistryState.UNAVAILABLE
    if any(records for records, _ in outcomes):
        return RegistryState.FOUND
    if any(error for _, error in outcomes):
        return RegistryState.UNAVAILABLE
    return RegistryState.NOT_FOUND


@dataclass(slots=True)
class RegistryResult:
    """Итог проверки по обоим реестрам."""

    state: str  # found | not_found | unavailable
    records: list[RegistryRecord] = field(default_factory=list)
    checked_at: dt.datetime = field(default_factory=lambda: dt.datetime.now(dt.UTC))
    errors: dict[str, str] = field(default_factory=dict)
    from_cache: bool = False

    @property
    def best(self) -> RegistryRecord | None:
        """Одна запись для отчёта: сначала действующая, затем свежий реестр."""
        if not self.records:
            return None
        return sorted(
            self.records,
            key=lambda r: (r.valid is not True, r.registry != "elk"),
        )[0]

    @property
    def unavailable(self) -> bool:
        return self.state == RegistryState.UNAVAILABLE

    def as_payload(self) -> dict[str, Any]:
        """Плоский вид для кэша и для колонки ``candidates.raw``."""
        return {
            "state": self.state,
            "errors": self.errors,
            "records": [
                {
                    "registry": r.registry,
                    "ru_number": r.ru_number,
                    "holder": r.holder,
                    "product_name": r.product_name,
                    "valid": r.valid,
                    "status_text": r.status_text,
                    "card_url": r.card_url,
                }
                for r in self.records
            ],
        }


def cache_key(name: str, ru_number: str | None) -> str:
    """Ключ кэша. Название нормализуется, чтобы регистр и пробелы не плодили копии."""
    normalised = " ".join(name.lower().split())
    payload = f"{normalised}|{(ru_number or '').strip().lower()}"
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


class RegistryService:
    """Клиент обоих реестров. Создаётся один раз на процесс."""

    def __init__(self) -> None:
        settings = get_settings()
        self._settings = settings
        # Реестры государственные и бесплатные, но таймаут и ретрай им нужны
        # не меньше платных: отвечают они медленно и не всегда.
        self._client = ApiClient(
            "registry",
            timeout_read=45.0,
            headers={
                "Accept": "application/json, text/html;q=0.9",
                # Заголовки кодируются в ascii — кириллица здесь уронила бы клиент.
                "User-Agent": "Medisent-Bot/0.1 (medical device procurement)",
            },
        )

    async def aclose(self) -> None:
        await self._client.aclose()

    # --- elk -------------------------------------------------------------

    async def _check_elk(
        self, name: str | None, ru_number: str | None, request_id: int | None
    ) -> tuple[list[RegistryRecord], str | None]:
        url = self._settings.registry_elk_base.rstrip("/") + endpoints.ELK_SEARCH_PATH
        result = await self._client.post(
            url,
            operation="elk.search",
            request_id=request_id,
            json=endpoints.build_elk_query(name=name, ru_number=ru_number),
        )
        if not result.ok:
            return [], result.error or "gateway elk недоступен"

        outcome = endpoints.parse_elk_payload(result.json)
        if not outcome.understood:
            # Ответ пришёл, но мы его не поняли. Это не «не найдено».
            return [], f"ответ elk не разобран: {outcome.note}"
        return outcome.records, None

    # --- misearch --------------------------------------------------------

    async def _check_misearch(
        self, name: str | None, ru_number: str | None, request_id: int | None
    ) -> tuple[list[RegistryRecord], str | None]:
        result = await self._client.get(
            self._settings.registry_misearch_url,
            operation="misearch.search",
            request_id=request_id,
            params=endpoints.build_misearch_params(name=name, ru_number=ru_number),
            expect_json=False,
        )
        if not result.ok:
            return [], result.error or "misearch недоступен"

        outcome = endpoints.parse_misearch_html(result.text)
        if not outcome.understood:
            return [], f"страница misearch не разобрана: {outcome.note}"
        return outcome.records, None

    # --- публичный интерфейс ---------------------------------------------

    async def check_product(
        self,
        name: str,
        ru_number: str | None = None,
        *,
        request_id: int | None = None,
        session: Any = None,
        cache: bool = False,
    ) -> RegistryResult:
        """Проверить изделие в обоих реестрах.

        Кэш: либо ``session`` — уже открытая сессия БД (тесты), либо
        ``cache=True`` — сервис сам открывает две короткие сессии, до и после
        обращения к реестрам. Держать одну сессию открытой на время HTTP-вызова
        нельзя: managed-база убивает соединения, простаивающие в транзакции.
        Без того и другого проверка отработает, просто без кэширования.
        """
        extra = log_extra(request_id)
        key = cache_key(name, ru_number)

        cached = None
        if session is not None:
            from bot.db import repo

            cached = await repo.get_registry_cache(session, key, self._settings.registry_cache_days)
        elif cache:
            from bot.db import repo
            from bot.db.session import session_scope

            async with session_scope() as own:
                cached = await repo.get_registry_cache(own, key, self._settings.registry_cache_days)
        if cached is not None:
            logger.info("Реестр: ответ из кэша для «%s»", name, extra=extra)
            payload = cached.payload
            return RegistryResult(
                state=cached.state,
                records=[
                    RegistryRecord(
                        registry=r.get("registry", "?"),
                        ru_number=r.get("ru_number"),
                        holder=r.get("holder"),
                        product_name=r.get("product_name"),
                        valid=r.get("valid"),
                        status_text=r.get("status_text"),
                        card_url=r.get("card_url"),
                        raw={},
                    )
                    for r in payload.get("records", [])
                ],
                checked_at=cached.checked_at,
                errors=payload.get("errors", {}),
                from_cache=True,
            )

        logger.info("Реестр: проверяю «%s» (РУ %s)", name, ru_number or "—", extra=extra)

        # Оба реестра опрашиваются параллельно: они независимы, и последовательный
        # обход удвоил бы и без того медленный ответ.
        elk_task = self._check_elk(name, ru_number, request_id)
        misearch_task = self._check_misearch(name, ru_number, request_id)
        (elk_records, elk_error), (mi_records, mi_error) = await asyncio.gather(
            elk_task, misearch_task
        )

        records = [*elk_records, *mi_records]
        errors: dict[str, str] = {}
        if elk_error:
            errors["elk"] = elk_error
        if mi_error:
            errors["misearch"] = mi_error

        # Логика статуса — в ``derive_state``, здесь только вызов.
        state = derive_state([(elk_records, elk_error), (mi_records, mi_error)])
        result = RegistryResult(state=state, records=records, errors=errors)

        if state == RegistryState.UNAVAILABLE:
            logger.warning("Реестр: проверить «%s» не удалось — %s", name, errors, extra=extra)
        else:
            logger.info("Реестр: «%s» → %s, записей %s", name, state, len(records), extra=extra)

        # Кэшируем только состоявшиеся проверки. Положить сюда unavailable
        # на 30 дней — значит месяц не проверять изделие.
        if state != RegistryState.UNAVAILABLE:
            if session is not None:
                from bot.db import repo

                await repo.put_registry_cache(session, key, state, result.as_payload())
            elif cache:
                from bot.db import repo
                from bot.db.session import session_scope

                async with session_scope() as own:
                    await repo.put_registry_cache(own, key, state, result.as_payload())

        return result

    async def check_unrega(
        self, name: str, holder: str | None = None, *, request_id: int | None = None
    ) -> RegistryResult:
        """Информационные письма об изъятиях. Негативный сигнал, не проверка РУ.

        Возвращает тот же ``RegistryResult``, что и проверка РУ: ``found`` —
        письма есть, ``not_found`` — их нет, ``unavailable`` — проверить не
        удалось. Раньше два последних случая склеивались в пустой список, и
        упавший сервис выглядел как «писем нет» — более сильное утверждение,
        чем бот вправе сделать.
        """
        extra = log_extra(request_id)
        query = f"{name} {holder}".strip() if holder else name
        result = await self._client.get(
            self._settings.registry_unrega_url,
            operation="unrega.search",
            request_id=request_id,
            params=endpoints.build_unrega_params(name=query),
            expect_json=False,
        )
        records: list[RegistryRecord] = []
        error: str | None = None
        if not result.ok:
            error = result.error or "unrega недоступен"
        else:
            outcome = endpoints.parse_unrega_html(result.text)
            if not outcome.understood:
                error = f"страница unrega не разобрана: {outcome.note}"
            else:
                records = outcome.records

        state = derive_state([(records, error)])
        if error:
            logger.warning("unrega: проверить «%s» не удалось — %s", name, error, extra=extra)
        else:
            logger.info("unrega: «%s» → %s, писем %s", name, state, len(records), extra=extra)
        return RegistryResult(
            state=state, records=records, errors={"unrega": error} if error else {}
        )


_service: RegistryService | None = None


def get_registry_service() -> RegistryService:
    global _service
    if _service is None:
        _service = RegistryService()
    return _service


async def close_registry_service() -> None:
    global _service
    if _service is not None:
        await _service.aclose()
    _service = None
