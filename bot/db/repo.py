"""Весь SQL проекта. Ни одного запроса в хендлерах или сервисах.

Правила, зашитые здесь:

* дедупликация поставщиков — только через частичные уникальные индексы и
  ``INSERT ... ON CONFLICT``; матчинга по названию компании нет нигде;
* чёрный список отсекается SQL-запросом **до** ранжирования и отчёта;
* пакетная запись кандидатов идёт одним многострочным INSERT, а не построчно.
"""

from __future__ import annotations

import datetime as dt
import hashlib
import json
import logging
from dataclasses import dataclass
from decimal import Decimal
from typing import Any

from sqlalchemy import Integer, Row, bindparam, cast, delete, func, select, text, update
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession

from bot.db.models import (
    ALLOWED_TRANSITIONS,
    ApiCall,
    Approval,
    ApprovalDecision,
    Blacklist,
    Candidate,
    Criterion,
    CriterionEvent,
    GmailState,
    Order,
    QuoteRequest,
    QuoteStatus,
    RegistryCache,
    Request,
    RequestStatus,
    Supplier,
)

# Реэкспорт: ключи дедупликации считаются одной функцией и здесь, и в поиске.
from bot.services.domains import normalise_domain as normalise_domain
from bot.services.domains import normalise_tax_id as normalise_tax_id

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
    # Лок снимается вместе с транзакцией; без него два одновременных запроса
    # посчитают один и тот же номер и второй упадёт на уникальном индексе.
    await session.execute(select(func.pg_advisory_xact_lock(func.hashtext("rfq_token"))))

    next_number: int | None = await session.scalar(
        select(
            func.coalesce(func.max(cast(func.split_part(Request.token, "-", 3), Integer)), 0) + 1
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
    row: Request | None = await session.get(Request, request_id)
    return row


async def get_request_by_token(session: AsyncSession, token: str) -> Request | None:
    row: Request | None = await session.scalar(select(Request).where(Request.token == token))
    return row


async def get_active_request(session: AsyncSession) -> Request | None:
    """Последняя незакрытая заявка — для ``/session`` и справочных ответов.

    Для привязки голосового выбора этого мало: см.
    ``list_requests_awaiting_choice``. «Последняя незакрытая» и «та, по которой
    показан отчёт» — разные вещи, если владелец завёл вторую заявку, не
    закрыв первую.
    """
    row: Request | None = await session.scalar(
        select(Request)
        .where(Request.status != RequestStatus.CLOSED)
        .order_by(Request.created_at.desc())
        .limit(1)
    )
    return row


async def list_requests_awaiting_choice(session: AsyncSession) -> list[Request]:
    """Все заявки, по которым показан отчёт и ждут выбора.

    Голосовое привязывается к заявке отсюда. Если таких заявок больше одной,
    угадывать нельзя: критерии уедут к чужим поставщикам, а исправить это
    потом будет нечем. Вызывающий код в этом случае спрашивает владельца.
    """
    return list(
        (
            await session.scalars(
                select(Request)
                .where(Request.status == RequestStatus.AWAITING_CHOICE)
                .order_by(Request.created_at)
            )
        ).all()
    )


async def transition(session: AsyncSession, request_id: int, to: str) -> bool:
    """Перевести заявку в статус ``to``, если из текущего это разрешено.

    Проверка и запись — одним UPDATE по ``ALLOWED_TRANSITIONS``: между SELECT
    статуса и UPDATE помещается /cancel владельца. ``False`` — переход не
    сделан; вызывающий решает, что это значит (обычно — заявку уже закрыли).
    """
    allowed_from = [status for status, targets in ALLOWED_TRANSITIONS.items() if to in targets]
    if not allowed_from:
        raise ValueError(f"в статус {to!r} не ведёт ни один переход")
    result = await session.execute(
        update(Request)
        .where(Request.id == request_id)
        .where(Request.status.in_(allowed_from))
        .values(status=to)
    )
    moved = bool(result.rowcount)
    if not moved:
        logger.warning("Заявка %s: переход в %s не разрешён из текущего статуса", request_id, to)
    return moved


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


async def upsert_suppliers(session: AsyncSession, suppliers: list[SupplierInput]) -> dict[str, int]:
    """Пакетно пишет поставщиков и возвращает ``{ключ: id}``.

    Ключ — нормализованный домен, иначе ИНН, иначе ``name``. Записи делятся на
    три группы, потому что ``ON CONFLICT`` умеет выводить только один индекс за
    раз, а у нас их два и оба частичные.

    Строки без домена и без ИНН пишутся как есть: склеивать их по названию
    запрещено, названия пишут по-разному.
    """
    if not suppliers:
        return {}

    # Внутри одного пакета ключ должен быть уникален: две строки с одним
    # lower(domain) в одном INSERT … ON CONFLICT DO UPDATE Postgres отвергает
    # целиком («cannot affect row a second time»). Повторы схлопываются
    # здесь, непустые поля дополняют друг друга.
    by_domain_map: dict[str, dict[str, Any]] = {}
    by_tax_map: dict[str, dict[str, Any]] = {}
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
            _merge_into(by_domain_map, payload["domain"], payload)
        elif payload["tax_id"]:
            _merge_into(by_tax_map, payload["tax_id"], payload)
        else:
            plain.append(payload)
    by_domain = list(by_domain_map.values())
    by_tax = list(by_tax_map.values())

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
        base = pg_insert(Supplier).values(by_domain)
        stmt_domain = base.on_conflict_do_update(
            index_elements=[func.lower(Supplier.domain)],
            index_where=text("domain IS NOT NULL"),
            set_=_update_set(base),
        ).returning(Supplier.id, Supplier.domain)
        for domain_row in (await session.execute(stmt_domain)).all():
            result[str(domain_row.domain)] = int(domain_row.id)

    if by_tax:
        base = pg_insert(Supplier).values(by_tax)
        stmt_tax = base.on_conflict_do_update(
            index_elements=[Supplier.tax_id],
            index_where=text("tax_id IS NOT NULL"),
            set_=_update_set(base),
        ).returning(Supplier.id, Supplier.tax_id)
        for tax_row in (await session.execute(stmt_tax)).all():
            result[str(tax_row.tax_id)] = int(tax_row.id)

    if plain:
        stmt_plain = pg_insert(Supplier).values(plain).returning(Supplier.id, Supplier.name)
        for plain_row in (await session.execute(stmt_plain)).all():
            result[str(plain_row.name)] = int(plain_row.id)

    return result


def _merge_into(bucket: dict[str, dict[str, Any]], key: str, payload: dict[str, Any]) -> None:
    """Второе появление того же ключа в пакете дополняет пустые поля первого."""
    existing = bucket.get(key)
    if existing is None:
        bucket[key] = payload
        return
    for field_name, value in payload.items():
        if existing.get(field_name) in (None, "") and value not in (None, ""):
            existing[field_name] = value


async def get_supplier(session: AsyncSession, supplier_id: int) -> Supplier | None:
    row: Supplier | None = await session.get(Supplier, supplier_id)
    return row


async def find_supplier_by_email(session: AsyncSession, email: str) -> Supplier | None:
    row: Supplier | None = await session.scalar(
        select(Supplier).where(func.lower(Supplier.email) == email.strip().lower()).limit(1)
    )
    return row


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


async def list_candidates_for_report(session: AsyncSession, request_id: int) -> list[Row[Any]]:
    """Кандидаты заявки **без тех, кто в чёрном списке**.

    Фильтр стоит здесь, в SQL, до всякого ранжирования: заблокированный
    поставщик не должен попасть в выдачу вообще, даже с лучшей ценой.
    """
    blacklisted = (
        select(Blacklist.supplier_id).where(Blacklist.lifted_at.is_(None)).scalar_subquery()
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
            # Флаг из guard: на странице поставщика нашёлся текст, похожий на
            # попытку повлиять на отбор. Достаём его здесь, чтобы он дошёл до
            # отчёта: детектор без последствий бесполезен.
            Candidate.raw["scrape"]["injection_suspected"]
            .as_boolean()
            .label("injection_suspected"),
            # Состояние проверки реестра — как его записал конвейер. Раньше
            # отчёт восстанавливал его из колонок ru_number/ru_checked_at и
            # «не смогли проверить» превращалось в «не нашли».
            Candidate.raw["registry"]["state"].as_string().label("registry_state"),
            Supplier.name.label("supplier_name"),
            Supplier.domain,
            Supplier.email,
            Supplier.phone,
        )
        .join(Supplier, Supplier.id == Candidate.supplier_id)
        .where(Candidate.request_id == request_id)
        .where(Candidate.supplier_id.not_in(blacklisted))
        # Порядок — тот, в котором владелец видел отчёт (``set_candidate_ranks``).
        # «Беру второго» считается по этому порядку, и он обязан совпадать с
        # нумерацией в сообщении, а не с порядком вставки строк.
        .order_by(
            func.coalesce(Candidate.raw["report"]["rank"].as_integer(), UNRANKED),
            Candidate.id,
        )
    )
    return list((await session.execute(stmt)).all())


UNRANKED = 1_000_000


async def set_candidate_ranks(session: AsyncSession, ranks: dict[int, int]) -> None:
    """Запомнить порядок отчёта: ``{candidate_id: позиция}``.

    Колонки ``candidates`` по ТЗ не расширяются, поэтому позиция лежит в
    ``raw.report.rank``. Пишется один раз при сборке отчёта; дальше по ней
    сортирует ``list_candidates_for_report``.
    """
    if not ranks:
        return
    patch = func.jsonb_build_object(
        "report", func.jsonb_build_object("rank", bindparam("rank", type_=Integer))
    )
    # Пакетный UPDATE по первичному ключу: ORM сам добавляет ``WHERE id = :id``
    # для каждого набора параметров, одним round-trip на всю заявку.
    stmt = update(Candidate).values(
        raw=func.coalesce(Candidate.raw, text("'{}'::jsonb")).op("||")(patch)
    )
    await session.execute(
        stmt, [{"id": candidate_id, "rank": rank} for candidate_id, rank in ranks.items()]
    )


async def is_selectable_candidate(session: AsyncSession, request_id: int, supplier_id: int) -> bool:
    """Есть ли поставщик среди кандидатов заявки и не в чёрном ли он списке.

    Через это проходит id, который вернула модель по голосовому. Модель может
    назвать номер вместо id, чужого поставщика или того, кого чёрный список
    уже отсёк из отчёта — письмо ни одному из них уходить не должно.
    """
    blacklisted = (
        select(Blacklist.supplier_id).where(Blacklist.lifted_at.is_(None)).scalar_subquery()
    )
    value = await session.scalar(
        select(func.count())
        .select_from(Candidate)
        .where(Candidate.request_id == request_id)
        .where(Candidate.supplier_id == supplier_id)
        .where(Candidate.supplier_id.not_in(blacklisted))
    )
    return bool(value)


async def count_blacklisted_in_request(session: AsyncSession, request_id: int) -> int:
    """Сколько кандидатов этой заявки отсеял чёрный список — для сообщения владельцу."""
    blacklisted = (
        select(Blacklist.supplier_id).where(Blacklist.lifted_at.is_(None)).scalar_subquery()
    )
    value = await session.scalar(
        select(func.count())
        .select_from(Candidate)
        .where(Candidate.request_id == request_id)
        .where(Candidate.supplier_id.in_(blacklisted))
    )
    return int(value or 0)


async def get_candidate(session: AsyncSession, candidate_id: int) -> Candidate | None:
    row: Candidate | None = await session.get(Candidate, candidate_id)
    return row


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
        select(
            Blacklist.id,
            Blacklist.supplier_id,
            Blacklist.reason,
            Blacklist.added_at,
            Supplier.name.label("supplier_name"),
        )
        .join(Supplier, Supplier.id == Blacklist.supplier_id)
        .where(Blacklist.lifted_at.is_(None))
        .order_by(Blacklist.added_at.desc())
    )
    return list((await session.execute(stmt)).all())


# --- Запросы цены и ответы ----------------------------------------------


async def find_quote(
    session: AsyncSession, request_id: int, supplier_id: int
) -> QuoteRequest | None:
    """Уже отправляли этому поставщику по этой заявке?"""
    row: QuoteRequest | None = await session.scalar(
        select(QuoteRequest)
        .where(QuoteRequest.request_id == request_id)
        .where(QuoteRequest.supplier_id == supplier_id)
        .limit(1)
    )
    return row


QUOTE_SENDING_STALE_MINUTES = 15


async def reserve_quote(
    session: AsyncSession, *, request_id: int, supplier_id: int, message_id: str
) -> QuoteRequest | None:
    """Занять пару «заявка + поставщик» **до** отправки письма.

    Уникальный индекс защищает строку, а не письмо: если строка появляется
    после ``send``, два одобрения на одну пару оба успевают отправить. Поэтому
    строка со статусом ``sending`` и заранее известным ``Message-ID`` пишется
    в той же транзакции, где занимается одобрение, и только потом идёт
    отправка.

    Конфликт по индексу: пара уже есть. Занять её заново можно только если
    прошлая попытка честно провалилась (``failed``) или зависла в ``sending``
    дольше ``QUOTE_SENDING_STALE_MINUTES`` — так падение процесса между
    резервом и отправкой не блокирует поставщика навсегда. Отправленное и
    отвеченное не перезанимается никогда; тогда возвращается ``None``.
    """
    stale = dt.datetime.now(dt.UTC) - dt.timedelta(minutes=QUOTE_SENDING_STALE_MINUTES)
    stmt = pg_insert(QuoteRequest).values(
        request_id=request_id,
        supplier_id=supplier_id,
        message_id=message_id,
        sent_at=dt.datetime.now(dt.UTC),
        status=QuoteStatus.SENDING,
    )
    reserve = stmt.on_conflict_do_update(
        constraint="quote_requests_request_supplier_uq",
        set_={
            "message_id": stmt.excluded.message_id,
            "gmail_thread": None,
            "sent_at": stmt.excluded.sent_at,
            "status": QuoteStatus.SENDING,
        },
        where=(QuoteRequest.status == QuoteStatus.FAILED)
        | ((QuoteRequest.status == QuoteStatus.SENDING) & (QuoteRequest.sent_at < stale)),
    ).returning(QuoteRequest.id)
    quote_id = await session.scalar(reserve)
    if quote_id is None:
        return None
    # populate_existing: строка могла уже лежать в identity map этой сессии
    # (повторный резерв после failed) — нужны свежие поля, а не кэш.
    return await session.get(QuoteRequest, quote_id, populate_existing=True)


async def mark_quote_sent(session: AsyncSession, quote_id: int, *, gmail_thread: str) -> None:
    await session.execute(
        update(QuoteRequest)
        .where(QuoteRequest.id == quote_id)
        .values(status=QuoteStatus.SENT, gmail_thread=gmail_thread, sent_at=dt.datetime.now(dt.UTC))
    )


async def mark_quote_failed(session: AsyncSession, quote_id: int) -> None:
    """Отправка не удалась. Пара освобождается для новой попытки, Message-ID
    остаётся: если Gmail всё же принял письмо, ответ на него привяжется."""
    await session.execute(
        update(QuoteRequest).where(QuoteRequest.id == quote_id).values(status=QuoteStatus.FAILED)
    )


async def find_quote_by_message_id(
    session: AsyncSession, message_ids: list[str]
) -> QuoteRequest | None:
    """Шаг 1 матчинга: In-Reply-To / References → наш Message-ID. Самый надёжный."""
    if not message_ids:
        return None
    row: QuoteRequest | None = await session.scalar(
        select(QuoteRequest).where(QuoteRequest.message_id.in_(message_ids)).limit(1)
    )
    return row


async def find_quote_by_thread(session: AsyncSession, thread_id: str) -> QuoteRequest | None:
    """Шаг 2 матчинга: threadId Gmail."""
    row: QuoteRequest | None = await session.scalar(
        select(QuoteRequest).where(QuoteRequest.gmail_thread == thread_id).limit(1)
    )
    return row


async def find_quote_by_token(session: AsyncSession, token: str) -> QuoteRequest | None:
    """Шаг 3 матчинга: токен в теме письма."""
    row: QuoteRequest | None = await session.scalar(
        select(QuoteRequest)
        .join(Request, Request.id == QuoteRequest.request_id)
        .where(Request.token == token)
        .order_by(QuoteRequest.sent_at.desc())
        .limit(1)
    )
    return row


async def find_quote_by_sender(session: AsyncSession, email: str) -> QuoteRequest | None:
    """Шаг 4 матчинга — последний. Адрес ненадёжен: отвечают из общей почты,
    через секретаря, с личного ящика. Берём только незакрытые запросы."""
    row: QuoteRequest | None = await session.scalar(
        select(QuoteRequest)
        .join(Supplier, Supplier.id == QuoteRequest.supplier_id)
        .where(func.lower(Supplier.email) == email.strip().lower())
        .where(QuoteRequest.replied_at.is_(None))
        .order_by(QuoteRequest.sent_at.desc())
        .limit(1)
    )
    return row


async def mark_quote_replied(
    session: AsyncSession, quote_id: int, *, reply_text: str, thread_id: str | None = None
) -> None:
    values: dict[str, Any] = {
        "replied_at": dt.datetime.now(dt.UTC),
        "reply_text": reply_text,
        "status": QuoteStatus.REPLIED,
    }
    if thread_id:
        values["gmail_thread"] = thread_id
    await session.execute(update(QuoteRequest).where(QuoteRequest.id == quote_id).values(**values))


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


async def list_silent_quotes(
    session: AsyncSession, older_than_hours: int = 72
) -> list[QuoteRequest]:
    """Отправленные без ответа. Основа для follow-up."""
    cutoff = dt.datetime.now(dt.UTC) - dt.timedelta(hours=older_than_hours)
    return list(
        (
            await session.scalars(
                select(QuoteRequest)
                .where(QuoteRequest.replied_at.is_(None))
                .where(QuoteRequest.sent_at < cutoff)
                .where(QuoteRequest.status == QuoteStatus.SENT)
            )
        ).all()
    )


async def get_quote(session: AsyncSession, quote_id: int) -> QuoteRequest | None:
    row: QuoteRequest | None = await session.get(QuoteRequest, quote_id)
    return row


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
    cached_tokens: int | None = None,
) -> None:
    session.add(
        ApiCall(
            service=service,
            operation=operation,
            request_id=request_id,
            tokens_in=tokens_in,
            tokens_out=tokens_out,
            cached_tokens=cached_tokens,
            cost_usd=cost_usd,
            status=status,
            duration_ms=duration_ms,
        )
    )


async def attach_orphan_api_calls(
    session: AsyncSession, request_id: int, *, operation_prefix: str, within_seconds: int = 300
) -> int:
    """Привязать к заявке свежие вызовы без заявки.

    Распознавание входа (фото, голос, файл) идёт до того, как заявка
    заведена, и его вызовы пишутся с ``request_id = NULL``. Заводить заявку
    до распознавания нельзя — нераспознанный ввод плодил бы пустые заявки и
    жёг номера RFQ. Поэтому после создания заявки её intake-вызовы за
    последние минуты приписываются ей: владелец один, вводы идут по одному.
    """
    cutoff = dt.datetime.now(dt.UTC) - dt.timedelta(seconds=within_seconds)
    result = await session.execute(
        update(ApiCall)
        .where(ApiCall.request_id.is_(None))
        .where(ApiCall.operation.like(f"{operation_prefix}%"))
        .where(ApiCall.created_at >= cutoff)
        .values(request_id=request_id)
    )
    return int(result.rowcount or 0)


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
    row: RegistryCache | None = await session.scalar(
        select(RegistryCache)
        .where(RegistryCache.cache_key == cache_key)
        .where(RegistryCache.checked_at >= cutoff)
        .limit(1)
    )
    return row


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
        set_={
            "state": stmt.excluded.state,
            "payload": stmt.excluded.payload,
            "checked_at": func.now(),
        },
    )
    await session.execute(stmt)


async def purge_registry_cache(session: AsyncSession, older_than_days: int) -> int:
    cutoff = dt.datetime.now(dt.UTC) - dt.timedelta(days=older_than_days)
    result = await session.execute(delete(RegistryCache).where(RegistryCache.checked_at < cutoff))
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


# --- Одобрения -----------------------------------------------------------


APPROVAL_TTL_HOURS = 24


def payload_hash(payload: dict[str, Any]) -> str:
    """Отпечаток показанного владельцу. Ключи сортируются, чтобы порядок полей
    не менял хеш."""
    canonical = json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


async def create_approval(
    session: AsyncSession,
    *,
    kind: str,
    payload: dict[str, Any],
    request_id: int | None = None,
    supplier_id: int | None = None,
    quote_id: int | None = None,
    ttl_hours: int = APPROVAL_TTL_HOURS,
) -> Approval:
    """Записать то, что показываем владельцу на подтверждение.

    Срок жизни нужен, потому что кнопки в Telegram не протухают: нажатие на
    кнопку недельной давности не должно ничего отправлять.
    """
    row = Approval(
        kind=kind,
        request_id=request_id,
        supplier_id=supplier_id,
        quote_id=quote_id,
        payload=payload,
        payload_hash=payload_hash(payload),
        expires_at=dt.datetime.now(dt.UTC) + dt.timedelta(hours=ttl_hours),
    )
    session.add(row)
    await session.flush()
    return row


async def claim_approval(
    session: AsyncSession, approval_id: int, *, decision: str
) -> Approval | None:
    """Атомарно занять одобрение под решение владельца.

    Возвращает строку, только если решение принимается впервые и срок не
    истёк. Повторная доставка callback, второе нажатие и просроченная кнопка
    получают ``None`` — и не приводят ко второй отправке.

    Проверка и запись сделаны одним UPDATE намеренно: между SELECT и UPDATE
    помещается второй callback.
    """
    stmt = (
        update(Approval)
        .where(Approval.id == approval_id)
        .where(Approval.decision.is_(None))
        .where(Approval.expires_at > func.now())
        .values(decision=decision, decided_at=func.now())
        .returning(Approval.id)
    )
    claimed = await session.scalar(stmt)
    if claimed is None:
        return None
    return await session.get(Approval, claimed)


async def get_approval(session: AsyncSession, approval_id: int) -> Approval | None:
    row: Approval | None = await session.get(Approval, approval_id)
    return row


async def mark_approval_applied(
    session: AsyncSession, approval_id: int, result: dict[str, Any]
) -> None:
    """Отметить, что одобренное действие выполнено, и чем оно закончилось."""
    await session.execute(
        update(Approval)
        .where(Approval.id == approval_id)
        .values(applied_at=func.now(), result=result)
    )


async def expire_stale_approvals(session: AsyncSession) -> int:
    """Пометить просроченные нерешённые одобрения. Чисто гигиена журнала."""
    result = await session.execute(
        update(Approval)
        .where(Approval.decision.is_(None))
        .where(Approval.expires_at <= func.now())
        .values(decision=ApprovalDecision.EXPIRED, decided_at=func.now())
    )
    return int(result.rowcount or 0)


async def spent_on_request(session: AsyncSession, request_id: int) -> Decimal:
    """Сколько уже потрачено по одной заявке. Основа потолка на заявку."""
    value = await session.scalar(
        select(func.coalesce(func.sum(ApiCall.cost_usd), 0)).where(ApiCall.request_id == request_id)
    )
    return Decimal(str(value or 0))


async def find_kp_approval(session: AsyncSession, quote_id: int) -> Approval | None:
    """Разбирали ли уже цены по этому запросу.

    Повторное письмо от того же поставщика не должно снова гонять модель:
    разговорчивый поставщик оплачивался бы столько раз, сколько раз ответил.
    """
    row: Approval | None = await session.scalar(
        select(Approval)
        .where(Approval.kind == "kp")
        .where(Approval.quote_id == quote_id)
        .order_by(Approval.created_at.desc())
        .limit(1)
    )
    return row
