"""Модели БД. Схема ровно из ТЗ плюс три служебные таблицы.

Служебные таблицы, которых нет в схеме ТЗ, но которых требует его же текст:

* ``api_calls``      — «каждый вызов платного API пишется в таблицу api_calls»
* ``registry_cache`` — «кэш результатов в Postgres на 30 дней» (этап 2)
* ``gmail_state``    — historyId между опросами Gmail (этап 7)

Колонки таблиц из ТЗ не расширяются: ТЗ — источник истины, и лишнее поле в
``candidates`` завтра разойдётся с тем, что читает Mathesar.
"""

from __future__ import annotations

import datetime as dt
from decimal import Decimal
from typing import Any

from sqlalchemy import (
    BigInteger,
    Boolean,
    CheckConstraint,
    Date,
    DateTime,
    Float,
    ForeignKey,
    Index,
    Integer,
    MetaData,
    Numeric,
    SmallInteger,
    String,
    Text,
    UniqueConstraint,
    func,
)
from sqlalchemy import (
    text as sql_text,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column

# Без naming_convention имена ограничений выдаёт база, и autogenerate начинает
# видеть несуществующие изменения (см. skill alembic-best-practices).
NAMING_CONVENTION = {
    "ix": "ix_%(column_0_label)s",
    "uq": "uq_%(table_name)s_%(column_0_N_name)s",
    "ck": "ck_%(table_name)s_%(constraint_name)s",
    "fk": "fk_%(table_name)s_%(column_0_name)s_%(referred_table_name)s",
    "pk": "pk_%(table_name)s",
}


class Base(DeclarativeBase):
    metadata = MetaData(naming_convention=NAMING_CONVENTION)


TZDateTime = DateTime(timezone=True)


# --- Статусы. Строками, как в ТЗ; CHECK не ставим, чтобы правка статуса из
# --- Mathesar не упиралась в ограничение.


class RequestStatus:
    SEARCH = "search"
    REPORT = "report"
    AWAITING_CHOICE = "awaiting_choice"
    MAIL_SENT = "mail_sent"
    AWAITING_REPLY = "awaiting_reply"
    KP = "kp"
    CLOSED = "closed"


class QuoteStatus:
    """Жизненный цикл письма поставщику.

    ``SENDING`` — пара занята, письмо ещё не ушло; ``FAILED`` — отправка
    не удалась, пару можно занять снова. Остальное — как в ТЗ.
    """

    SENDING = "sending"
    FAILED = "failed"
    SENT = "sent"
    REPLIED = "replied"
    SILENT = "silent"
    REFUSED = "refused"

    # Письмо реально уходило: второе по той же паре не отправляется.
    DELIVERED = frozenset({"sent", "replied", "silent", "refused"})


class RegistryState:
    """Три исхода проверки. ``UNAVAILABLE`` — «не смогли проверить», и это не
    то же самое, что ``NOT_FOUND`` — «проверили, не нашли»."""

    FOUND = "found"
    NOT_FOUND = "not_found"
    UNAVAILABLE = "unavailable"


# --- Таблицы из ТЗ -------------------------------------------------------


class Request(Base):
    """Сессия подбора. Без неё непонятно, к чему относится «беру второго»."""

    __tablename__ = "requests"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    created_at: Mapped[dt.datetime] = mapped_column(
        TZDateTime, nullable=False, server_default=func.now()
    )
    raw_input: Mapped[str | None] = mapped_column(Text)
    input_kind: Mapped[str | None] = mapped_column(Text)  # text | photo | voice | file
    product: Mapped[str] = mapped_column(Text, nullable=False)
    status: Mapped[str] = mapped_column(Text, nullable=False)
    token: Mapped[str] = mapped_column(Text, nullable=False, unique=True)


class Supplier(Base):
    __tablename__ = "suppliers"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    name: Mapped[str] = mapped_column(Text, nullable=False)
    domain: Mapped[str | None] = mapped_column(Text)
    tax_id: Mapped[str | None] = mapped_column(Text)  # ИНН
    country: Mapped[str | None] = mapped_column(Text)
    email: Mapped[str | None] = mapped_column(Text)
    phone: Mapped[str | None] = mapped_column(Text)
    first_seen: Mapped[dt.datetime] = mapped_column(
        TZDateTime, nullable=False, server_default=func.now()
    )
    found_via: Mapped[str | None] = mapped_column(Text)

    # Дедупликация целиком на этих двух индексах. Матчинг по названию компании
    # не делаем: названия пишут по-разному.
    #
    # Индексы частичные. Следствие, которое надо помнить: строки с NULL в
    # domain (или tax_id) под ограничение не попадают, и двух поставщиков без
    # домена и без ИНН база примет молча — это ожидаемо, а не дырка.
    __table_args__ = (
        Index(
            "suppliers_domain_uq",
            sql_text("lower(domain)"),
            unique=True,
            postgresql_where=sql_text("domain IS NOT NULL"),
        ),
        Index(
            "suppliers_tax_uq",
            "tax_id",
            unique=True,
            postgresql_where=sql_text("tax_id IS NOT NULL"),
        ),
    )


class Candidate(Base):
    """Результат проверки поставщика по конкретной заявке."""

    __tablename__ = "candidates"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    request_id: Mapped[int | None] = mapped_column(BigInteger, ForeignKey("requests.id"))
    supplier_id: Mapped[int | None] = mapped_column(BigInteger, ForeignKey("suppliers.id"))

    # Что поставщик заявляет у себя на сайте. К реестру отношения не имеет.
    site_claims: Mapped[bool | None] = mapped_column(Boolean)
    site_url: Mapped[str | None] = mapped_column(Text)
    site_price: Mapped[Decimal | None] = mapped_column(Numeric)

    # Что говорит реестр Росздравнадзора про само изделие. К поставщику
    # отношения не имеет: дилеров в реестре нет вообще.
    ru_number: Mapped[str | None] = mapped_column(Text)
    ru_holder: Mapped[str | None] = mapped_column(Text)
    ru_valid: Mapped[bool | None] = mapped_column(Boolean)
    ru_registry: Mapped[str | None] = mapped_column(Text)  # misearch | elk
    ru_checked_at: Mapped[dt.datetime | None] = mapped_column(TZDateTime)

    unrega_flags: Mapped[dict[str, Any] | None] = mapped_column(JSONB)
    raw: Mapped[dict[str, Any] | None] = mapped_column(JSONB)

    # Пара «заявка + поставщик» уникальна: повторный обход того же сайта
    # обновляет строку, а не плодит вторую (пакетный upsert в repo.py).
    __table_args__ = (
        UniqueConstraint("request_id", "supplier_id", name="candidates_request_supplier_uq"),
        Index("ix_candidates_request_id", "request_id"),
    )


class QuoteRequest(Base):
    __tablename__ = "quote_requests"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    request_id: Mapped[int | None] = mapped_column(BigInteger, ForeignKey("requests.id"))
    supplier_id: Mapped[int | None] = mapped_column(BigInteger, ForeignKey("suppliers.id"))
    sent_at: Mapped[dt.datetime | None] = mapped_column(TZDateTime)
    gmail_thread: Mapped[str | None] = mapped_column(Text)
    message_id: Mapped[str | None] = mapped_column(Text)  # RFC Message-ID отправленного
    replied_at: Mapped[dt.datetime | None] = mapped_column(TZDateTime)
    reply_text: Mapped[str | None] = mapped_column(Text)
    price: Mapped[Decimal | None] = mapped_column(Numeric)
    currency: Mapped[str | None] = mapped_column(Text)
    lead_time: Mapped[str | None] = mapped_column(Text)
    status: Mapped[str | None] = mapped_column(Text)

    # Матчинг ответов ищет по этим трём полям, каждый поиск — точечный.
    #
    # Уникальность пары «заявка + поставщик» держит база, а не проверка в коде:
    # повторный выбор того же поставщика голосовым не должен отправить второе
    # письмо, а ловить это условием в хендлере — значит проиграть гонку.
    __table_args__ = (
        UniqueConstraint("request_id", "supplier_id", name="quote_requests_request_supplier_uq"),
        Index("ix_quote_requests_message_id", "message_id"),
        Index("ix_quote_requests_gmail_thread", "gmail_thread"),
        Index("ix_quote_requests_request_id", "request_id"),
    )


class Order(Base):
    __tablename__ = "orders"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    supplier_id: Mapped[int | None] = mapped_column(BigInteger, ForeignKey("suppliers.id"))
    ordered_at: Mapped[dt.date | None] = mapped_column(Date)
    items: Mapped[dict[str, Any] | None] = mapped_column(JSONB)
    amount: Mapped[Decimal | None] = mapped_column(Numeric)
    currency: Mapped[str | None] = mapped_column(Text)
    promised_date: Mapped[dt.date | None] = mapped_column(Date)
    actual_date: Mapped[dt.date | None] = mapped_column(Date)
    rating: Mapped[int | None] = mapped_column(SmallInteger)

    __table_args__ = (CheckConstraint("rating BETWEEN 1 AND 5", name="rating_range"),)


class Blacklist(Base):
    """Отдельная таблица, не флажок: нужны причина, дата и история."""

    __tablename__ = "blacklist"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    supplier_id: Mapped[int | None] = mapped_column(BigInteger, ForeignKey("suppliers.id"))
    added_at: Mapped[dt.datetime] = mapped_column(
        TZDateTime, nullable=False, server_default=func.now()
    )
    reason: Mapped[str] = mapped_column(Text, nullable=False)
    order_id: Mapped[int | None] = mapped_column(BigInteger, ForeignKey("orders.id"))
    lifted_at: Mapped[dt.datetime | None] = mapped_column(TZDateTime)

    __table_args__ = (Index("ix_blacklist_supplier_id", "supplier_id"),)


class Criterion(Base):
    """Самопополняемая база критериев выбора."""

    __tablename__ = "criteria"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    text: Mapped[str] = mapped_column(Text, nullable=False)
    direction: Mapped[str | None] = mapped_column(Text)  # plus | minus
    weight: Mapped[float] = mapped_column(Float, server_default=sql_text("1.0"))
    times_seen: Mapped[int] = mapped_column(Integer, server_default=sql_text("1"))
    first_seen: Mapped[dt.datetime] = mapped_column(TZDateTime, server_default=func.now())
    last_seen: Mapped[dt.datetime] = mapped_column(TZDateTime, server_default=func.now())


class CriterionEvent(Base):
    """Каждое появление критерия вместе с сырым голосовым.

    Транскрипт хранится обязательно: через полгода странный критерий надо
    будет чем-то объяснить.
    """

    __tablename__ = "criteria_events"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    criterion_id: Mapped[int | None] = mapped_column(BigInteger, ForeignKey("criteria.id"))
    request_id: Mapped[int | None] = mapped_column(BigInteger, ForeignKey("requests.id"))
    supplier_id: Mapped[int | None] = mapped_column(BigInteger, ForeignKey("suppliers.id"))
    transcript: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[dt.datetime] = mapped_column(TZDateTime, server_default=func.now())


# --- Служебные таблицы ---------------------------------------------------


class ApiCall(Base):
    """Учёт расходов. Строка на каждый вызов внешнего сервиса."""

    __tablename__ = "api_calls"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    created_at: Mapped[dt.datetime] = mapped_column(
        TZDateTime, nullable=False, server_default=func.now()
    )
    service: Mapped[str] = mapped_column(Text, nullable=False)  # gemini | perplexity | ...
    operation: Mapped[str | None] = mapped_column(Text)
    request_id: Mapped[int | None] = mapped_column(BigInteger, ForeignKey("requests.id"))
    tokens_in: Mapped[int | None] = mapped_column(Integer)
    tokens_out: Mapped[int | None] = mapped_column(Integer)
    # Сколько токенов промпта модель взяла из своего кэша. Без этой цифры не
    # видно, работает ли стабильный префикс промпта.
    cached_tokens: Mapped[int | None] = mapped_column(Integer)
    cost_usd: Mapped[Decimal | None] = mapped_column(Numeric(12, 6))
    status: Mapped[str | None] = mapped_column(Text)  # ok | error | timeout
    duration_ms: Mapped[int | None] = mapped_column(Integer)

    __table_args__ = (
        Index("ix_api_calls_created_at", "created_at"),
        Index("ix_api_calls_request_id", "request_id"),
    )


class RegistryCache(Base):
    """Кэш ответов реестра на 30 дней. Реестр меняется медленно, лимиты беречь надо."""

    __tablename__ = "registry_cache"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    cache_key: Mapped[str] = mapped_column(String(512), nullable=False, unique=True)
    state: Mapped[str] = mapped_column(Text, nullable=False)  # found | not_found
    payload: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False)
    checked_at: Mapped[dt.datetime] = mapped_column(
        TZDateTime, nullable=False, server_default=func.now()
    )

    # Строки со state='unavailable' сюда не пишутся вообще: кэшировать
    # «не смогли проверить» на 30 дней — значит месяц не проверять.
    __table_args__ = (Index("ix_registry_cache_checked_at", "checked_at"),)


class GmailState(Base):
    """historyId последнего разобранного среза почты. Строка ровно одна."""

    __tablename__ = "gmail_state"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=False)
    history_id: Mapped[str | None] = mapped_column(Text)
    updated_at: Mapped[dt.datetime] = mapped_column(TZDateTime, server_default=func.now())


class ApprovalKind:
    EMAIL = "email"
    KP = "kp"


class ApprovalDecision:
    APPROVED = "approved"
    REJECTED = "rejected"
    EXPIRED = "expired"


class Approval(Base):
    """Что именно владелец одобрил, дословно.

    Раньше черновик письма и разобранные цены жили в словаре модуля: перезапуск
    контейнера — и нажатие «Да» упиралось в пустоту. Теперь одобряемое лежит
    в базе, и отправка берёт адресата и текст **отсюда**, а не перечитывает
    поставщика заново. Иначе правка записи между показом и подтверждением
    отправила бы письмо по адресу, которого владелец не видел.

    ``payload_hash`` — отпечаток показанного. По нему видно в логах, что ушло
    ровно то, что показывали.
    """

    __tablename__ = "approvals"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    kind: Mapped[str] = mapped_column(Text, nullable=False)  # email | kp
    request_id: Mapped[int | None] = mapped_column(BigInteger, ForeignKey("requests.id"))
    supplier_id: Mapped[int | None] = mapped_column(BigInteger, ForeignKey("suppliers.id"))
    quote_id: Mapped[int | None] = mapped_column(BigInteger, ForeignKey("quote_requests.id"))

    payload: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False)
    payload_hash: Mapped[str] = mapped_column(String(64), nullable=False)

    created_at: Mapped[dt.datetime] = mapped_column(
        TZDateTime, nullable=False, server_default=func.now()
    )
    expires_at: Mapped[dt.datetime] = mapped_column(TZDateTime, nullable=False)
    decided_at: Mapped[dt.datetime | None] = mapped_column(TZDateTime)
    decision: Mapped[str | None] = mapped_column(Text)
    applied_at: Mapped[dt.datetime | None] = mapped_column(TZDateTime)
    result: Mapped[dict[str, Any] | None] = mapped_column(JSONB)

    __table_args__ = (
        Index("ix_approvals_request_id", "request_id"),
        Index("ix_approvals_pending", "kind", "decision"),
    )
