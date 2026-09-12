"""Оркестрация подбора: поиск → скрейп → реестр → кандидаты → отчёт.

Оркестратор — код бота. Ни n8n, ни CRM в схеме нет.

Порядок шагов важен: проверка в реестре идёт по изделию один раз, а не по
каждому поставщику, потому что реестр отвечает про изделие и про поставщиков
не знает ничего. Дилеров в реестре нет вообще.

Сессии базы здесь короткие и открываются только вокруг записи. Внешние вызовы
(поиск, реестры, обход сайтов) идут минутами, и держать на это время открытую
транзакцию нельзя: managed-база убивает соединения, простаивающие в
транзакции, и финальный коммит падал бы уже после того, как деньги потрачены.
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Sequence
from dataclasses import dataclass, field
from decimal import Decimal
from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession

from bot.db import repo
from bot.db.models import RegistryState, RequestStatus
from bot.db.repo import CandidateInput, SupplierInput
from bot.db.session import session_scope
from bot.logging_setup import log_extra
from bot.services import budget, guard
from bot.services import criteria as criteria_service
from bot.services.firecrawl import ScrapeResult, get_firecrawl_service
from bot.services.perplexity import get_perplexity_service
from bot.services.registry import RegistryResult, get_registry_service
from bot.services.report import CandidateView, Report, rank_candidates

logger = logging.getLogger(__name__)

MAX_SITES_TO_SCRAPE = 10


@dataclass(slots=True)
class SearchSummary:
    total_found: int = 0
    blacklisted: int = 0
    scraped: int = 0
    registry_state: str = RegistryState.UNAVAILABLE
    unrega_state: str = RegistryState.UNAVAILABLE
    errors: list[str] = field(default_factory=list)
    # Поиск не отработал (сервис упал, потолок, ключ). Это не «ничего не
    # нашли»: владельцу нельзя советовать «уточните название», когда виноват
    # не он.
    search_failed: bool = False
    # Заявка упёрлась в потолок расходов: часть сайтов осталась непроверенной,
    # и владелец должен об этом знать, а не гадать, почему кандидатов мало.
    budget_exceeded: bool = False


async def run_search(
    *,
    request_id: int,
    product: str,
    requirements: list[str],
) -> SearchSummary:
    """Найти поставщиков, проверить изделие в реестрах, записать кандидатов."""
    extra = log_extra(request_id)
    summary = SearchSummary()
    registry_service = get_registry_service()

    # 1. Поиск поставщиков и проверка изделия в реестре — независимы, идут
    #    параллельно. Реестр отвечает медленно, ждать его последовательно
    #    незачем. Кэш реестра сервис читает и пишет своими короткими сессиями.
    search_task = get_perplexity_service().find_suppliers(
        product, requirements=requirements, request_id=request_id
    )
    registry_task = registry_service.check_product(product, request_id=request_id, cache=True)
    search, registry = await asyncio.gather(search_task, registry_task)

    summary.registry_state = registry.state
    if not search.ok:
        summary.search_failed = True
        summary.budget_exceeded = budget.exceeded(request_id)
        summary.errors.append(search.error or "поиск не удался")
        logger.warning("Поиск не удался: %s", search.error, extra=extra)
        return summary
    if registry.unavailable:
        summary.errors.append("реестр недоступен")

    summary.total_found = len(search.suppliers)
    if not search.suppliers:
        return summary

    # 2. Поставщики в базу через upsert. Дедупликация — на уникальных
    #    индексах, матчинга по названию нет.
    # Названия компаний придумала модель поиска по содержимому чужих страниц.
    # Прогоняем их через тот же фильтр, что и скрейп: поставщик с названием
    # «Медтехника. Ignore previous instructions» не должен попасть в промпт
    # отчёта как обычное поле.
    tainted_suppliers: set[str] = set()
    supplier_inputs: list[SupplierInput] = []
    for item in search.suppliers:
        screening = guard.screen_third_party(f"{item.name} {item.note}", source="выдача поиска")
        if screening.suspicious:
            tainted_suppliers.add(item.site or item.name)
            logger.warning(
                "Подозрительное название поставщика «%s»: %s",
                item.name,
                screening.summary,
                extra=extra,
            )
        supplier_inputs.append(
            SupplierInput(
                name=item.name,
                domain=item.site or None,
                email=item.email or None,
                phone=item.phone or None,
                found_via=search.query[:500],
            )
        )
    async with session_scope() as session:
        key_to_id = await repo.upsert_suppliers(session, supplier_inputs)

    # 3. Обход сайтов и информационные письма — параллельно, обе задачи
    #    внешние и друг от друга не зависят. Список сайтов ограничен: десяток
    #    — уже пара минут и заметные деньги, а кандидатов сверх десяти
    #    владелец всё равно не читает.
    best = registry.best
    to_scrape = [item for item in search.suppliers if item.site][:MAX_SITES_TO_SCRAPE]
    scrape_task = (
        get_firecrawl_service().scrape_many(
            [item.site for item in to_scrape], product, request_id=request_id
        )
        if to_scrape
        else _nothing()
    )
    unrega_task = registry_service.check_unrega(
        product, holder=best.holder if best else None, request_id=request_id
    )
    results, unrega = await asyncio.gather(scrape_task, unrega_task)
    scrapes: dict[str, ScrapeResult] = {result.url: result for result in results}
    summary.scraped = sum(1 for r in results if r.ok)
    summary.unrega_state = unrega.state
    if unrega.unavailable:
        summary.errors.append("информационные письма не проверены")

    # 4. Кандидаты одним пакетом.
    candidates: list[CandidateInput] = []
    for item in search.suppliers:
        supplier_id = _resolve_supplier_id(item, key_to_id)
        if supplier_id is None:
            continue
        scrape = scrapes.get(item.site) if item.site else None
        candidates.append(
            CandidateInput(
                supplier_id=supplier_id,
                site_claims=scrape.claims_stock if scrape and scrape.ok else None,
                site_url=item.site or None,
                site_price=scrape.price if scrape and scrape.ok else None,
                # Поля реестра относятся к изделию и одинаковы у всех
                # кандидатов заявки. Это не «поставщик проверен» — это
                # «изделие зарегистрировано».
                ru_number=best.ru_number if best else None,
                ru_holder=best.holder if best else None,
                ru_valid=best.valid if best else None,
                ru_registry=best.registry if best else None,
                # «Проверяли» — только если проверка состоялась. При
                # ``unavailable`` колонка остаётся пустой: заполненная дата
                # рядом с пустым номером читалась бы как «проверили, не нашли».
                ru_checked_at=None if registry.unavailable else registry.checked_at,
                unrega_flags=_unrega_flags(unrega),
                raw={
                    "registry": registry.as_payload(),
                    "search": {"note": item.note, "source": item.source_url},
                    "scrape": {
                        "ok": bool(scrape and scrape.ok),
                        "error": scrape.error if scrape else None,
                        # Подозрение может прийти с двух сторон: из текста
                        # страницы и из названия, придуманного поиском.
                        "injection_suspected": bool(
                            (scrape and scrape.injection_suspected)
                            or (item.site or item.name) in tainted_suppliers
                        ),
                    },
                },
            )
        )

    async with session_scope() as session:
        if candidates:
            await repo.upsert_candidates(session, request_id, candidates)
        # Контакты, найденные на сайте, дополняют то, что дал поиск.
        await _enrich_contacts(session, search.suppliers, scrapes, key_to_id)
        summary.blacklisted = await repo.count_blacklisted_in_request(session, request_id)
        await repo.set_request_status(session, request_id, RequestStatus.REPORT)

    summary.budget_exceeded = budget.exceeded(request_id)
    logger.info(
        "Подбор: найдено %s, обойдено сайтов %s, реестр %s, письма %s, отсеяно чёрным списком %s",
        summary.total_found,
        summary.scraped,
        summary.registry_state,
        summary.unrega_state,
        summary.blacklisted,
        extra=extra,
    )
    return summary


async def _nothing() -> list[ScrapeResult]:
    """Заглушка на место обхода сайтов, когда обходить нечего."""
    return []


def _unrega_flags(unrega: RegistryResult) -> dict[str, Any]:
    """Колонка ``candidates.unrega_flags``: состояние проверки плюс сами письма.

    Состояние здесь обязательно: пустой список без него не отличим от
    «не проверяли», а это разные факты и в отчёте они печатаются по-разному.
    """
    return {
        "state": unrega.state,
        "items": [r.product_name or r.status_text or "письмо" for r in unrega.records],
        "errors": unrega.errors,
    }


def _resolve_supplier_id(item: object, key_to_id: dict[str, int]) -> int | None:
    """Найти id поставщика по тому же ключу, каким его писал upsert."""
    site = getattr(item, "site", "") or ""
    name = getattr(item, "name", "") or ""
    if site:
        domain = repo.normalise_domain(site)
        if domain and domain in key_to_id:
            return key_to_id[domain]
    return key_to_id.get(name.strip())


async def _enrich_contacts(
    session: AsyncSession,
    suppliers: Sequence[object],
    scrapes: dict[str, ScrapeResult],
    key_to_id: dict[str, int],
) -> None:
    """Дописать e-mail и телефон, найденные скрейпом.

    Идёт тем же upsert'ом: COALESCE не затрёт уже известный контакт пустотой.
    """
    updates: list[SupplierInput] = []
    for item in suppliers:
        site = getattr(item, "site", "") or ""
        scrape = scrapes.get(site)
        if not scrape or not scrape.ok:
            continue
        if not scrape.email and not scrape.phone:
            continue
        updates.append(
            SupplierInput(
                name=str(getattr(item, "name", "")),
                domain=site or None,
                email=scrape.email,
                phone=scrape.phone,
            )
        )
    if updates:
        await repo.upsert_suppliers(session, updates)


async def build_report(
    session: AsyncSession,
    *,
    request_id: int,
    product: str,
    qty: str,
    requirements: list[str],
) -> Report:
    """Собрать отчёт по кандидатам заявки.

    Чёрный список отсекается внутри запроса ``list_candidates_for_report`` —
    до всякого ранжирования, а не после.
    """
    request = await repo.get_request(session, request_id)
    token = request.token if request else "?"

    rows = await repo.list_candidates_for_report(session, request_id)
    views = [
        CandidateView(
            candidate_id=int(row.id),
            supplier_id=int(row.supplier_id or 0),
            supplier_name=str(row.supplier_name),
            domain=row.domain,
            email=row.email,
            phone=row.phone,
            site_claims=row.site_claims,
            site_url=row.site_url,
            site_price=row.site_price if isinstance(row.site_price, Decimal) else None,
            ru_number=row.ru_number,
            ru_holder=row.ru_holder,
            ru_valid=row.ru_valid,
            ru_registry=row.ru_registry,
            registry_state=_registry_state(row),
            unrega_flags=_unrega_items(row.unrega_flags),
            injection_suspected=bool(row.injection_suspected),
        )
        for row in rows
    ]
    unrega_state = _unrega_state(rows)

    known_criteria = await criteria_service.for_prompt(session)
    ordered, summary, missing, failed = await rank_candidates(
        views,
        product=product,
        qty=qty,
        requirements=requirements,
        criteria=known_criteria,
        request_id=request_id,
        unrega_state=unrega_state,
    )

    # Порядок отчёта — факт, а не деталь рендера: по нему владелец скажет
    # «беру второго». Сохраняем, чтобы выбор считался по той же нумерации.
    await repo.set_candidate_ranks(
        session, {view.candidate_id: index for index, view in enumerate(ordered, start=1)}
    )
    await repo.set_request_status(session, request_id, RequestStatus.AWAITING_CHOICE)
    return Report(
        request_token=token,
        product=product,
        candidates=ordered,
        summary=summary,
        missing_data=missing,
        llm_failed=failed,
        unrega_state=unrega_state,
    )


def _registry_state(row: object) -> str:
    """Состояние проверки реестра — как его записал конвейер.

    Читается из ``raw.registry.state``. Восстанавливать его из колонок нельзя:
    ``ru_checked_at`` без ``ru_number`` неотличим от «проверили и не нашли»,
    и «реестр недоступен» превращалось бы в «РУ не найдено».

    Записи без сохранённого состояния (их не бывает у строк, записанных
    конвейером) считаются непроверенными: это единственное, что про них
    известно честно.
    """
    state = getattr(row, "registry_state", None)
    if state in (RegistryState.FOUND, RegistryState.NOT_FOUND, RegistryState.UNAVAILABLE):
        return str(state)
    if getattr(row, "ru_number", None):
        return RegistryState.FOUND
    return RegistryState.UNAVAILABLE


def _unrega_items(value: object) -> list[str]:
    if isinstance(value, dict):
        return [str(item) for item in value.get("items", []) or []]
    return []


def _unrega_state(rows: Sequence[Any]) -> str:
    """Состояние проверки писем — одно на заявку, как и проверка РУ."""
    for row in rows:
        flags = getattr(row, "unrega_flags", None)
        if isinstance(flags, dict) and flags.get("state") in (
            RegistryState.FOUND,
            RegistryState.NOT_FOUND,
            RegistryState.UNAVAILABLE,
        ):
            return str(flags["state"])
    return RegistryState.UNAVAILABLE if rows else RegistryState.NOT_FOUND
