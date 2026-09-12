"""Оркестрация подбора: поиск → скрейп → реестр → кандидаты → отчёт.

Оркестратор — код бота. Ни n8n, ни CRM в схеме нет.

Порядок шагов важен: проверка в реестре идёт по изделию один раз, а не по
каждому поставщику, потому что реестр отвечает про изделие и про поставщиков
не знает ничего. Дилеров в реестре нет вообще.
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Sequence
from dataclasses import dataclass, field
from decimal import Decimal

from sqlalchemy.ext.asyncio import AsyncSession

from bot.db import repo
from bot.db.models import RegistryState, RequestStatus
from bot.db.repo import CandidateInput, SupplierInput
from bot.logging_setup import log_extra
from bot.services import budget, guard
from bot.services import criteria as criteria_service
from bot.services.firecrawl import ScrapeResult, get_firecrawl_service
from bot.services.perplexity import get_perplexity_service
from bot.services.registry import get_registry_service
from bot.services.report import CandidateView, Report, rank_candidates

logger = logging.getLogger(__name__)

MAX_SITES_TO_SCRAPE = 10


@dataclass(slots=True)
class SearchSummary:
    total_found: int = 0
    blacklisted: int = 0
    scraped: int = 0
    registry_state: str = RegistryState.UNAVAILABLE
    errors: list[str] = field(default_factory=list)
    # Заявка упёрлась в потолок расходов: часть сайтов осталась непроверенной,
    # и владелец должен об этом знать, а не гадать, почему кандидатов мало.
    budget_exceeded: bool = False


async def run_search(
    session: AsyncSession,
    *,
    request_id: int,
    product: str,
    qty: str,
    requirements: list[str],
) -> SearchSummary:
    """Найти поставщиков, проверить изделие в реестре, записать кандидатов."""
    extra = log_extra(request_id)
    summary = SearchSummary()

    # 1. Поиск поставщиков и проверка изделия в реестре — независимы, идут
    #    параллельно. Реестр отвечает медленно, ждать его последовательно
    #    незачем.
    search_task = get_perplexity_service().find_suppliers(
        product, requirements=requirements, request_id=request_id
    )
    registry_task = get_registry_service().check_product(
        product, request_id=request_id, session=session
    )
    search, registry = await asyncio.gather(search_task, registry_task)

    summary.registry_state = registry.state
    if not search.ok:
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
    key_to_id = await repo.upsert_suppliers(session, supplier_inputs)

    # 3. Обход сайтов. Ограничиваем список: десяток сайтов — уже пара минут и
    #    заметные деньги, а кандидатов сверх десяти владелец всё равно не
    #    читает.
    to_scrape = [item for item in search.suppliers if item.site][:MAX_SITES_TO_SCRAPE]
    scrapes: dict[str, ScrapeResult] = {}
    if to_scrape:
        results = await get_firecrawl_service().scrape_many(
            [item.site for item in to_scrape], product, request_id=request_id
        )
        scrapes = {result.url: result for result in results}
        summary.scraped = sum(1 for r in results if r.ok)

    # 4. Кандидаты одним пакетом.
    best = registry.best
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
                ru_checked_at=registry.checked_at,
                unrega_flags=None,
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

    if candidates:
        await repo.upsert_candidates(session, request_id, candidates)

    # Контакты, найденные на сайте, дополняют то, что дал поиск.
    await _enrich_contacts(session, search.suppliers, scrapes, key_to_id)

    summary.budget_exceeded = budget.exceeded(request_id)
    summary.blacklisted = await repo.count_blacklisted_in_request(session, request_id)
    await repo.set_request_status(session, request_id, RequestStatus.REPORT)

    logger.info(
        "Подбор: найдено %s, обойдено сайтов %s, реестр %s, отсеяно чёрным списком %s",
        summary.total_found,
        summary.scraped,
        summary.registry_state,
        summary.blacklisted,
        extra=extra,
    )
    return summary


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
            registry_state=_state_from_row(row),
            unrega_flags=(
                list((row.unrega_flags or {}).get("items", []))
                if isinstance(row.unrega_flags, dict)
                else []
            ),
        )
        for row in rows
    ]

    known_criteria = await criteria_service.for_prompt(session)
    ordered, summary, missing, failed = await rank_candidates(
        views,
        product=product,
        qty=qty,
        requirements=requirements,
        criteria=known_criteria,
        request_id=request_id,
    )

    await repo.set_request_status(session, request_id, RequestStatus.AWAITING_CHOICE)
    return Report(
        request_token=token,
        product=product,
        candidates=ordered,
        summary=summary,
        missing_data=missing,
        llm_failed=failed,
    )


def _state_from_row(row: object) -> str:
    """Восстановить состояние проверки из записанных полей.

    Проверено и найдено → found. Проверено и не найдено → not_found.
    Не проверялось вовсе → unavailable, потому что «не смогли проверить» —
    это именно то, что произошло.
    """
    if getattr(row, "ru_number", None):
        return RegistryState.FOUND
    if getattr(row, "ru_checked_at", None):
        return RegistryState.NOT_FOUND
    return RegistryState.UNAVAILABLE
