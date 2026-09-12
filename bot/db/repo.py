"""Весь SQL проекта. Ни одного запроса в хендлерах или сервисах.

Правила, зашитые здесь:

* дедупликация поставщиков — только через частичные уникальные индексы и
  ``INSERT ... ON CONFLICT``; матчинга по названию компании нет нигде;
* чёрный список отсекается SQL-запросом **до** ранжирования и отчёта;
* пакетная запись кандидатов идёт одним многострочным INSERT, а не построчно.
"""

from __future__ import annotations

import datetime as dt
import logging
from dataclasses import dataclass
from decimal import Decimal
from typing import Any

from sqlalchemy import Row, delete, func, select, text, update
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession

from bot.db.models import (
    ApiCall,
    Blacklist,
    Candidate,
    Criterion,
    CriterionEvent,
    GmailState,
    Order,
    QuoteRequest,
    RegistryCache,
    Request,
    RequestStatus,
    Supplier,
)

logger = logging.getLogger(__name__)

# Порог схожести для склейки критериев по смыслу. Выше — считаем тем же
# критерием. Намеренно высокий: ложная склейка портит вес критерия навсегда,
# а лишняя строка не портит ничего.
CRITERIA_SIMILARITY_THRESHOLD = 0.62


# --- Заявки --------------------------------------------------------------


async def create_request(
    session: AsyncSession,
    *,
    product: str,
    raw_input: str | None,
    input_kind: str,
) -> Request:
    """Создаёт заявку и присваивает ей токен ``RFQ-<год>-<счётчик>``.

    Счётчик считается внутри транзакции под advisory-локом: без него два
    одновременных запроса получат один и тот же номер.
    """
    year = dt.datetime.now(dt.UTC).year
    await session.execute(select(func.pg_advisory_xact_lock(func.hashtext("rfq_token"))))

    next_number = await session.scalar(
        select(
            func.coalesce(func.max(func.split_part(Request.token, "-", 3).cast(func.Integer())), 0)
            + 1
        ).where(Request.token.like(f"RFQ-{year}-%"))
    )
    token = f"RFQ-{year}-{int(next_number or 1):03d}"

    row = Request(
        product=product,
        raw_input=raw_input,
        input_kind=input_kind,
        status=RequestStatus.SEARCH,
        token=token,
    )
    session.add(row)
    await session.flush()
    return row


async def get_request(session: AsyncSession, request_id: int) -> Request | None:
    return await session.get(Request, request_id)


async def get_request_by_token(session: AsyncSession, token: str) -> Request | None:
    return await session.scalar(select(Request).where(Request.token == token))


async def get_active_request(session: AsyncSession) -> Request | None:
    """Последняя незакрытая заявка. Владелец один, параллельных сессий нет."""
    return await session.scalar(
        select(Request)
        .where(Request.status != RequestStatus.CLOSED)
        .order_by(Request.created_at.desc())
        .limit(1)
    )


async def set_request_status(session: AsyncSession, request_id: int, status: str) -> None:
    await session.execute(
        update(Request).where(Request.id == request_id).values(status=status)
    )


# --- Поставщики ----------------------------------------------------------


@dataclass(slots=True)
class SupplierInput:
    """Поставщик, каким его вернул поиск. Домен нормализуется перед записью."""

    name: str
    domain: str | None = None
    tax_id: str | None = None
    country: str | None = None
    email: str | None = None
    phone: str | None = None
    found_via: str | None = None


def normalise_domain(value: str | None) -> str | None:
    """``https://WWW.Example.RU/catalog?x=1`` → ``example.ru``.

    Уникальный индекс стоит на ``lower(domain)``, но срезать схему, ``www.``
    и путь база за нас не станет — это делается здесь, до записи.
    """
    if not value:
        return None
    cleaned = value.strip().lower()
    for prefix in ("https://", "http://"):
        if cleaned.startswith(prefix):
            cleaned = cleaned[len(prefix) :]
    cleaned = cleaned.split("/", 1)[0].split("?", 1)[0].split("#", 1)[0]
    if cleaned.startswith("www."):
        cleaned = cleaned[4:]
    cleaned = cleaned.rstrip(".")
    return cleaned or None


def normalise_tax_id(value: str | None) -> str | None:
    """ИНН — только цифры. 10 знаков у юрлица, 12 у ИП; иное отбрасываем."""
    if not value:
        return None
    digits = "".join(ch for ch in value if ch.isdigit())
    return digits if len(digits) in (10, 12) else None


async def upsert_suppliers(
    session: AsyncSession, suppliers: list[SupplierInput]
) -> dict[str, int]:
    """Пакетно пишет поставщиков и возвращает ``{ключ: id}``.

    Ключ — нормализованный домен, иначе ИНН, иначе ``name``. Записи делятся на
    три группы, потому что ``ON CONFLICT`` умеет выводить только один индекс за
    раз, а у нас их два и оба частичные.

    Строки без домена и без ИНН пишутся как есть: склеивать их по названию
    запрещено, названия пишут по-разному.
    """
    if not suppliers:
        return {}

    by_domain: list[dict[str, Any]] = []
    by_tax: list[dict[str, Any]] = []
    plain: list[dict[str, Any]] = []

    for item in suppliers:
        payload: dict[str, Any] = {
            "name": item.name.strip(),
            "domain": normalise_domain(item.domain),
            "tax_id": normalise_tax_id(item.tax_id),
            "country": item.country,
            "email": item.email,
            "phone": item.phone,
            "found_via": item.found_via,
        }
        if payload["domain"]:
            by_domain.append(payload)
        elif payload["tax_id"]:
            by_tax.append(payload)
        else:
            plain.append(payload)

    result: dict[str, int] = {}

    # COALESCE(EXCLUDED.x, suppliers.x): повторная находка дополняет пустые
    # поля, но не затирает уже известный контакт пустотой.
    def _update_set(table: Any) -> dict[str, Any]:
        return {
            "name": func.coalesce(table.excluded.name, Supplier.name),
            "email": func.coalesce(table.excluded.email, Supplier.email),
            "phone": func.coalesce(table.excluded.phone, Supplier.phone),
            "tax_id": func.coalesce(table.excluded.tax_id, Supplier.tax_id),
            "country": func.coalesce(table.excluded.country, Supplier.country),
        }

    if by_domain:
        stmt = pg_insert(Supplier).values(by_domain)
        stmt = stmt.on_conflict_do_update(
            index_elements=[func.lower(Supplier.domain)],
            index_where=text("domain IS NOT NULL"),
            set_=_update_set(stmt),
        ).returning(Supplier.id, Supplier.domain)
        for row in (await session.execute(stmt)).all():
            result[str(row.domain)] = int(row.id)

    if by_tax:
        stmt = pg_insert(Supplier).values(by_tax)
        stmt = stmt.on_conflict_do_update(
            index_elements=[Supplier.tax_id],
            index_where=text("tax_id IS NOT NULL"),
            set_=_update_set(stmt),
        ).returning(Supplier.id, Supplier.tax_id)
        for row in (await session.execute(stmt)).all():
            result[str(row.tax_id)] = int(row.id)

    if plain:
        stmt_plain = pg_insert(Supplier).values(plain).returning(Supplier.id, Supplier.name)
        for row in (await session.execute(stmt_plain)).all():
            result[str(row.name)] = int(row.id)

    return result


async def get_supplier(session: AsyncSession, supplier_id: int) -> Supplier | None:
    return await session.get(Supplier, supplier_id)


async def find_supplier_by_email(session: AsyncSession, email: str) -> Supplier | None:
    return await session.scalar(
        select(Supplier).where(func.lower(Supplier.email) == email.strip().lower()).limit(1)
    )


# --- Кандидаты -----------------------------------------------------------


@dataclass(slots=True)
class CandidateInput:
    supplier_id: int
    site_claims: bool | None = None
    site_url: str | None = None
    site_price: Decimal | None = None
    ru_number: str | None = None
    ru_holder: str | None = None
    ru_valid: bool | None = None
    ru_registry: str | None = None
    ru_checked_at: dt.datetime | None = None
    unrega_flags: dict[str, Any] | None = None
    raw: dict[str, Any] | None = None


async def upsert_candidates(
    session: AsyncSession, request_id: int, candidates: list[CandidateInput]
) -> int:
    """Одним многострочным INSERT ... ON CONFLICT DO UPDATE.

    Построчная запись здесь стоила бы round-trip на каждого кандидата; при
    managed-базе с сетевой задержкой это заметно даже на десятке строк.
    """
    if not candidates:
        return 0

    rows = [
        {
            "request_id": request_id,
            "supplier_id": c.supplier_id,
            "site_claims": c.site_claims,
            "site_url": c.site_url,
            "site_price": c.site_price,
            "ru_number": c.ru_number,
            "ru_holder": c.ru_holder,
            "ru_valid": c.ru_valid,
            "ru_registry": c.ru_registry,
            "ru_checked_at": c.ru_checked_at,
            "unrega_flags": c.unrega_flags,
            "raw": c.raw,
        }
        for c in candidates
    ]

    stmt = pg_insert(Candidate).values(rows)
    stmt = stmt.on_conflict_do_update(
        constraint="candidates_request_supplier_uq",
        set_={
            "site_claims": stmt.excluded.site_claims,
            "site_url": stmt.excluded.site_url,
            "site_price": stmt.excluded.site_price,
            "ru_number": stmt.excluded.ru_number,
            "ru_holder": stmt.excluded.ru_holder,
            "ru_valid": stmt.excluded.ru_valid,
            "ru_registry": stmt.excluded.ru_registry,
            "ru_checked_at": stmt.excluded.ru_checked_at,
            "unrega_flags": stmt.excluded.unrega_flags,
            "raw": stmt.excluded.raw,
        },
    )
    await session.execute(stmt)
    return len(rows)


async def list_candidates_for_report(
    session: AsyncSession, request_id: int
) -> list[Row[Any]]:
    """Кандидаты заявки **без тех, кто в чёрном списке**.

    Фильтр стоит здесь, в SQL, до всякого ранжирования: заблокированный
    поставщик не должен попасть в выдачу вообще, даже с лучшей ценой.
    """
    blacklisted = (
        select(Blacklist.supplier_id)
        .where(Blacklist.lifted_at.is_(None))
        .scalar_subquery()
    )
    stmt = (
        select(
            Candidate.id,
            Candidate.supplier_id,
            Candidate.site_claims,
            Candidate.site_url,
            Candidate.site_price,
            Candidate.ru_number,
            Candidate.ru_holder,
            Candidate.ru_valid,
            Candidate.ru_registry,
            Candidate.ru_checked_at,
            Candidate.unrega_flags,
            Supplier.name.label("supplier_name"),
            Supplier.domain,
            Supplier.email,
            Supplier.phone,
        )
        .join(Supplier, Supplier.id == Candidate.supplier_id)
        .where(Candidate.request_id == request_id)
        .where(Candidate.supplier_id.not_in(blacklisted))
        .order_by(Candidate.id)
    )
    return list((await session.execute(stmt)).all())


async def count_blacklisted_in_request(session: AsyncSession, request_id: int) -> int:
    """Сколько кандидатов этой заявки отсеял чёрный список — для сообщения владельцу."""
    blacklisted = (
        select(Blacklist.supplier_id)
        .where(Blacklist.lifted_at.is_(None))
        .scalar_subquery()
    )
    value = await session.scalar(
        select(func.count())
        .select_from(Candidate)
        .where(Candidate.request_id == request_id)
        .where(Candidate.supplier_id.in_(blacklisted))
    )
    return int(value or 0)


async def get_candidate(session: AsyncSession, candidate_id: int) -> Candidate | None:
    return await session.get(Candidate, candidate_id)


# --- Чёрный список -------------------------------------------------------


async def add_to_blacklist(
    session: AsyncSession, supplier_id: int, reason: str, order_id: int | None = None
) -> Blacklist:
    row = Blacklist(supplier_id=supplier_id, reason=reason, order_id=order_id)
    session.add(row)
    await session.flush()
    return row


async def lift_from_blacklist(session: AsyncSession, supplier_id: int) -> bool:
    result = await session.execute(
        update(Blacklist)
        .where(Blacklist.supplier_id == supplier_id, Blacklist.lifted_at.is_(None))
        .values(lifted_at=func.now())
    )
    return bool(result.rowcount)


async def list_blacklist(session: AsyncSession) -> list[Row[Any]]:
    stmt = (
        select(Blacklist.id, Blacklist.supplier_id, Blacklist.reason, Blacklist.added_at,
               Supplier.name.label("supplier_name"))
        .join(Supplier, Supplier.id == Blacklist.supplier_id)
        .where(Blacklist.lifted_at.is_(None))
        .order_by(Blacklist.added_at.desc())
    )
    return list((await session.execute(stmt)).all())


# --- Запросы цены и ответы ----------------------------------------------


async def create_quote_request(
    session: AsyncSession,
    *,
    request_id: int,
    supplier_id: int,
    gmail_thread: str | None,
    message_id: str | None,
) -> QuoteRequest:
    row = QuoteRequest(
        request_id=request_id,
        supplier_id=supplier_id,
        gmail_thread=gmail_thread,
        message_id=message_id,
        sent_at=dt.datetime.now(dt.UTC),
        status="sent",
    )
    session.add(row)
    await session.flush()
    return row


async def find_quote_by_message_id(
    session: AsyncSession, message_ids: list[str]
) -> QuoteRequest | None:
    """Шаг 1 матчинга: In-Reply-To / References → наш Message-ID. Самый надёжный."""
    if not message_ids:
        return None
    return await session.scalar(
        select(QuoteRequest).where(QuoteRequest.message_id.in_(message_ids)).limit(1)
    )


async def find_quote_by_thread(session: AsyncSession, thread_id: str) -> QuoteRequest | None:
    """Шаг 2 матчинга: threadId Gmail."""
    return await session.scalar(
        select(QuoteRequest).where(QuoteRequest.gmail_thread == thread_id).limit(1)
    )


async def find_quote_by_token(session: AsyncSession, token: str) -> QuoteRequest | None:
    """Шаг 3 матчинга: токен в теме письма."""
    return await session.scalar(
        select(QuoteRequest)
        .join(Request, Request.id == QuoteRequest.request_id)
        .where(Request.token == token)
        .order_by(QuoteRequest.sent_at.desc())
        .limit(1)
    )


async def find_quote_by_sender(session: AsyncSession, email: str) -> QuoteRequest | None:
    """Шаг 4 матчинга — последний. Адрес ненадёжен: отвечают из общей почты,
    через секретаря, с личного ящика. Берём только незакрытые запросы."""
    return await session.scalar(
        select(QuoteRequest)
        .join(Supplier, Supplier.id == QuoteRequest.supplier_id)
        .where(func.lower(Supplier.email) == email.strip().lower())
        .where(QuoteRequest.replied_at.is_(None))
        .order_by(QuoteRequest.sent_at.desc())
        .limit(1)
    )


async def mark_quote_replied(
    session: AsyncSession, quote_id: int, *, reply_text: str, thread_id: str | None = None
) -> None:
    values: dict[str, Any] = {
        "replied_at": dt.datetime.now(dt.UTC),
        "reply_text": reply_text,
        "status": "replied",
    }
    if thread_id:
        values["gmail_thread"] = thread_id
    await session.execute(
        update(QuoteRequest).where(QuoteRequest.id == quote_id).values(**values)
    )


async def set_quote_prices(
    session: AsyncSession,
    quote_id: int,
    *,
    price: Decimal | None,
    currency: str | None,
    lead_time: str | None,
) -> None:
    await session.execute(
        update(QuoteRequest)
        .where(QuoteRequest.id == quote_id)
        .values(price=price, currency=currency, lead_time=lead_time)
    )


async def list_silent_quotes(session: AsyncSession, older_than_hours: int = 72) -> list[QuoteRequest]:
    """Отправленные без ответа. Основа для follow-up."""
    cutoff = dt.datetime.now(dt.UTC) - dt.timedelta(hours=older_than_hours)
    return list(
        (
            await session.scalars(
                select(QuoteRequest)
                .where(QuoteRequest.replied_at.is_(None))
                .where(QuoteRequest.sent_at < cutoff)
                .where(QuoteRequest.status == "sent")
            )
        ).all()
    )


async def get_quote(session: AsyncSession, quote_id: int) -> QuoteRequest | None:
    return await session.get(QuoteRequest, quote_id)


# --- Критерии ------------------------------------------------------------


async def _trigram_available(session: AsyncSession) -> bool:
    value = await session.scalar(
        select(func.count()).select_from(text("pg_extension")).where(text("extname = 'pg_trgm'"))
    )
    return bool(value)


async def upsert_criterion(
    session: AsyncSession,
    *,
    text_value: str,
    direction: str | None,
    weight: float,
    same_as: int | None = None,
) -> tuple[int, bool]:
    """Склейка критерия по смыслу. Возвращает ``(id, создан_новый)``.

    Порядок поиска совпадения, от надёжного к рискованному:

    1. ``same_as`` — модель прямо указала на известный критерий;
    2. точное совпадение текста без учёта регистра;
    3. триграммная схожесть выше порога, если стоит ``pg_trgm``.

    Не нашли — заводим новую строку. Это сознательный выбор: ложная склейка
    портит вес критерия навсегда, а лишняя строка чинится в Mathesar за минуту.
    """
    cleaned = " ".join(text_value.split())
    if not cleaned:
        raise ValueError("пустой текст критерия")

    match_id: int | None = None

    if same_as is not None:
        match_id = await session.scalar(select(Criterion.id).where(Criterion.id == same_as))

    if match_id is None:
        match_id = await session.scalar(
            select(Criterion.id).where(func.lower(Criterion.text) == cleaned.lower()).limit(1)
        )

    if match_id is None and await _trigram_available(session):
        similarity = func.similarity(Criterion.text, cleaned)
        match_id = await session.scalar(
            select(Criterion.id)
            .where(similarity >= CRITERIA_SIMILARITY_THRESHOLD)
            .order_by(similarity.desc())
            .limit(1)
        )

    if match_id is not None:
        await session.execute(
            update(Criterion)
            .where(Criterion.id == match_id)
            .values(
                times_seen=Criterion.times_seen + 1,
                last_seen=func.now(),
                # Вес усредняем со старым, чтобы одно эмоциональное «это
                # принципиально» не перевесило десять спокойных упоминаний.
                weight=(Criterion.weight + weight) / 2,
            )
        )
        return int(match_id), False

    row = Criterion(text=cleaned, direction=direction, weight=weight, times_seen=1)
    session.add(row)
    await session.flush()
    return int(row.id), True


async def record_criterion_event(
    session: AsyncSession,
    *,
    criterion_id: int,
    request_id: int | None,
    supplier_id: int | None,
    transcript: str | None,
) -> None:
    """Сырое голосовое сохраняется обязательно — иначе странный критерий
    через полгода нечем будет объяснить."""
    session.add(
        CriterionEvent(
            criterion_id=criterion_id,
            request_id=request_id,
            supplier_id=supplier_id,
            transcript=transcript,
        )
    )


async def list_criteria(session: AsyncSession, limit: int = 30) -> list[Criterion]:
    """Накопленные критерии для подмешивания в промпт отчёта."""
    return list(
        (
            await session.scalars(
                select(Criterion)
                .order_by((Criterion.weight * Criterion.times_seen).desc())
                .limit(limit)
            )
        ).all()
    )


# --- Учёт расходов -------------------------------------------------------


async def record_api_call(
    session: AsyncSession,
    *,
    service: str,
    operation: str | None,
    request_id: int | None,
    tokens_in: int | None,
    tokens_out: int | None,
    cost_usd: Decimal | None,
    status: str,
    duration_ms: int | None,
) -> None:
    session.add(
        ApiCall(
            service=service,
            operation=operation,
            request_id=request_id,
            tokens_in=tokens_in,
            tokens_out=tokens_out,
            cost_usd=cost_usd,
            status=status,
            duration_ms=duration_ms,
        )
    )


async def spent_today(session: AsyncSession) -> Decimal:
    value = await session.scalar(
        select(func.coalesce(func.sum(ApiCall.cost_usd), 0)).where(
            ApiCall.created_at >= func.date_trunc("day", func.now())
        )
    )
    return Decimal(str(value or 0))


async def stats_today(session: AsyncSession) -> list[Row[Any]]:
    stmt = (
        select(
            ApiCall.service,
            func.count().label("calls"),
            func.coalesce(func.sum(ApiCall.cost_usd), 0).label("cost"),
        )
        .where(ApiCall.created_at >= func.date_trunc("day", func.now()))
        .group_by(ApiCall.service)
        .order_by(func.coalesce(func.sum(ApiCall.cost_usd), 0).desc())
    )
    return list((await session.execute(stmt)).all())


# --- Кэш реестра ---------------------------------------------------------


async def get_registry_cache(
    session: AsyncSession, cache_key: str, max_age_days: int
) -> RegistryCache | None:
    cutoff = dt.datetime.now(dt.UTC) - dt.timedelta(days=max_age_days)
    return await session.scalar(
        select(RegistryCache)
        .where(RegistryCache.cache_key == cache_key)
        .where(RegistryCache.checked_at >= cutoff)
        .limit(1)
    )


async def put_registry_cache(
    session: AsyncSession, cache_key: str, state: str, payload: dict[str, Any]
) -> None:
    """Кэшируются только состоявшиеся проверки.

    ``unavailable`` сюда не попадает никогда: закэшировать «не смогли
    проверить» на 30 дней — значит месяц не проверять.
    """
    stmt = pg_insert(RegistryCache).values(
        cache_key=cache_key, state=state, payload=payload, checked_at=func.now()
    )
    stmt = stmt.on_conflict_do_update(
        index_elements=[RegistryCache.cache_key],
        set_={"state": stmt.excluded.state, "payload": stmt.excluded.payload,
              "checked_at": func.now()},
    )
    await session.execute(stmt)


async def purge_registry_cache(session: AsyncSession, older_than_days: int) -> int:
    cutoff = dt.datetime.now(dt.UTC) - dt.timedelta(days=older_than_days)
    result = await session.execute(
        delete(RegistryCache).where(RegistryCache.checked_at < cutoff)
    )
    return int(result.rowcount or 0)


# --- Состояние Gmail -----------------------------------------------------


async def get_gmail_history_id(session: AsyncSession) -> str | None:
    row = await session.get(GmailState, 1)
    return row.history_id if row else None


async def set_gmail_history_id(session: AsyncSession, history_id: str) -> None:
    stmt = pg_insert(GmailState).values(id=1, history_id=history_id, updated_at=func.now())
    stmt = stmt.on_conflict_do_update(
        index_elements=[GmailState.id],
        set_={"history_id": stmt.excluded.history_id, "updated_at": func.now()},
    )
    await session.execute(stmt)


# --- Заказы --------------------------------------------------------------


async def list_orders_for_supplier(session: AsyncSession, supplier_id: int) -> list[Order]:
    return list(
        (
            await session.scalars(
                select(Order)
                .where(Order.supplier_id == supplier_id)
                .order_by(Order.ordered_at.desc())
            )
        ).all()
    )
