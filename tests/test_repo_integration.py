"""Интеграционные тесты repo.py на живой базе.

Пропускаются, если ``TEST_DATABASE_URL`` не задан — на машине без Postgres
остальные тесты должны проходить всё равно.

**База в ``TEST_DATABASE_URL`` должна быть отдельной от рабочей.** Каждый тест
сносит и создаёт схему заново; если направить его на базу, которой управляет
Alembic, таблицы исчезнут, а ``alembic_version`` останется — и следующий
``alembic upgrade`` решит, что всё уже применено.

Проверяется то, что нельзя проверить моком: генерация токена под конкурентной
нагрузкой, поведение частичных уникальных индексов и SQL-фильтр чёрного
списка.
"""

from __future__ import annotations

import asyncio
import os
from collections.abc import AsyncIterator

import pytest
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from bot.db import repo
from bot.db.models import Base, RequestStatus
from bot.db.repo import CandidateInput, SupplierInput

TEST_DB = os.environ.get("TEST_DATABASE_URL")

pytestmark = pytest.mark.skipif(
    not TEST_DB, reason="TEST_DATABASE_URL не задан — интеграционные тесты пропущены"
)


@pytest.fixture
async def session() -> AsyncIterator[AsyncSession]:
    """Чистая схема на каждый тест: создаём, отдаём сессию, сносим."""
    engine = create_async_engine(str(TEST_DB), poolclass=None)
    async with engine.begin() as connection:
        await connection.run_sync(Base.metadata.drop_all)
        await connection.run_sync(Base.metadata.create_all)
    maker = async_sessionmaker(engine, expire_on_commit=False)
    async with maker() as active:
        yield active
        await active.rollback()
    async with engine.begin() as connection:
        await connection.run_sync(Base.metadata.drop_all)
    await engine.dispose()


# --- Токен заявки --------------------------------------------------------


async def test_token_is_sequential_within_year(session: AsyncSession) -> None:
    first = await repo.create_request(session, product="Тонометр", raw_input="", input_kind="text")
    second = await repo.create_request(session, product="Термометр", raw_input="", input_kind="text")
    await session.commit()

    assert first.token.endswith("-001")
    assert second.token.endswith("-002")
    assert first.token.startswith("RFQ-")


async def test_concurrent_requests_get_distinct_tokens() -> None:
    """Advisory-лок против гонки: два одновременных запроса не получают один номер.

    Именно этот путь ломался тихо — генерация номера через max()+1 без лока
    даёт двум транзакциям одинаковый счётчик, и вторая падает на уникальном
    индексе уже после того, как владелец увидел «Принял».
    """
    if not TEST_DB:
        pytest.skip("нет базы")

    engine = create_async_engine(str(TEST_DB))
    async with engine.begin() as connection:
        await connection.run_sync(Base.metadata.drop_all)
        await connection.run_sync(Base.metadata.create_all)
    maker = async_sessionmaker(engine, expire_on_commit=False)

    async def make(index: int) -> str:
        async with maker() as active:
            row = await repo.create_request(
                active, product=f"Изделие {index}", raw_input="", input_kind="text"
            )
            await active.commit()
            return row.token

    tokens = await asyncio.gather(*(make(i) for i in range(5)))

    async with engine.begin() as connection:
        await connection.run_sync(Base.metadata.drop_all)
    await engine.dispose()

    assert len(set(tokens)) == 5, f"токены повторились: {tokens}"


# --- Дедупликация поставщиков -------------------------------------------


async def test_same_domain_different_case_is_one_supplier(session: AsyncSession) -> None:
    first = await repo.upsert_suppliers(
        session, [SupplierInput(name="Первый", domain="https://WWW.Example.RU/catalog")]
    )
    second = await repo.upsert_suppliers(
        session, [SupplierInput(name="Он же", domain="example.ru", email="sales@example.ru")]
    )
    await session.commit()

    assert first["example.ru"] == second["example.ru"]
    supplier = await repo.get_supplier(session, first["example.ru"])
    assert supplier is not None
    assert supplier.email == "sales@example.ru"   # контакт дописался


async def test_upsert_does_not_erase_known_contact_with_empty(session: AsyncSession) -> None:
    """COALESCE: повторная находка без e-mail не должна затирать найденный ранее."""
    await repo.upsert_suppliers(
        session, [SupplierInput(name="А", domain="a.ru", email="sales@a.ru", phone="+7 900 000")]
    )
    ids = await repo.upsert_suppliers(session, [SupplierInput(name="А", domain="a.ru")])
    await session.commit()

    supplier = await repo.get_supplier(session, ids["a.ru"])
    assert supplier is not None
    assert supplier.email == "sales@a.ru"
    assert supplier.phone == "+7 900 000"


async def test_suppliers_without_domain_and_tax_id_do_not_collide(session: AsyncSession) -> None:
    """Частичные индексы: две строки с NULL под ограничение не попадают.

    Это ожидаемое поведение, а не дырка: склеивать поставщиков по названию
    запрещено, названия пишут по-разному.
    """
    ids = await repo.upsert_suppliers(
        session, [SupplierInput(name="ООО Первый"), SupplierInput(name="ООО Второй")]
    )
    await session.commit()
    assert len(set(ids.values())) == 2


async def test_same_tax_id_is_one_supplier(session: AsyncSession) -> None:
    first = await repo.upsert_suppliers(session, [SupplierInput(name="А", tax_id="7701234567")])
    second = await repo.upsert_suppliers(
        session, [SupplierInput(name="А, но иначе написано", tax_id="7701234567")]
    )
    await session.commit()
    assert first["7701234567"] == second["7701234567"]


async def test_invalid_tax_id_is_dropped_not_stored(session: AsyncSession) -> None:
    """ИНН бывает 10 или 12 цифр. «12345» — мусор, и уникальность по нему
    склеила бы разных поставщиков."""
    ids = await repo.upsert_suppliers(
        session,
        [SupplierInput(name="А", tax_id="12345"), SupplierInput(name="Б", tax_id="не указан")],
    )
    await session.commit()
    assert len(set(ids.values())) == 2


# --- Чёрный список -------------------------------------------------------


async def test_blacklisted_supplier_never_reaches_the_report(session: AsyncSession) -> None:
    """Фильтр стоит в SQL до ранжирования: заблокированного нет в выдаче вообще."""
    request = await repo.create_request(
        session, product="Тонометр", raw_input="", input_kind="text"
    )
    ids = await repo.upsert_suppliers(
        session,
        [
            SupplierInput(name="Хороший", domain="good.ru"),
            SupplierInput(name="Плохой", domain="bad.ru"),
        ],
    )
    await repo.upsert_candidates(
        session,
        int(request.id),
        [CandidateInput(supplier_id=ids["good.ru"]), CandidateInput(supplier_id=ids["bad.ru"])],
    )
    await repo.add_to_blacklist(session, ids["bad.ru"], "сорвал сроки дважды")
    await session.commit()

    rows = await repo.list_candidates_for_report(session, int(request.id))
    names = {row.supplier_name for row in rows}

    assert names == {"Хороший"}
    assert await repo.count_blacklisted_in_request(session, int(request.id)) == 1


async def test_lifted_supplier_comes_back(session: AsyncSession) -> None:
    request = await repo.create_request(session, product="Т", raw_input="", input_kind="text")
    ids = await repo.upsert_suppliers(session, [SupplierInput(name="Б", domain="b.ru")])
    await repo.upsert_candidates(
        session, int(request.id), [CandidateInput(supplier_id=ids["b.ru"])]
    )
    await repo.add_to_blacklist(session, ids["b.ru"], "просрочка")
    await session.commit()
    assert await repo.list_candidates_for_report(session, int(request.id)) == []

    await repo.lift_from_blacklist(session, ids["b.ru"])
    await session.commit()
    assert len(await repo.list_candidates_for_report(session, int(request.id))) == 1


# --- Кандидаты -----------------------------------------------------------


async def test_repeated_scrape_updates_candidate_not_duplicates(session: AsyncSession) -> None:
    from decimal import Decimal

    request = await repo.create_request(session, product="Т", raw_input="", input_kind="text")
    ids = await repo.upsert_suppliers(session, [SupplierInput(name="А", domain="a.ru")])
    await repo.upsert_candidates(
        session,
        int(request.id),
        [CandidateInput(supplier_id=ids["a.ru"], site_price=Decimal("100"))],
    )
    await repo.upsert_candidates(
        session,
        int(request.id),
        [CandidateInput(supplier_id=ids["a.ru"], site_price=Decimal("120"))],
    )
    await session.commit()

    rows = await repo.list_candidates_for_report(session, int(request.id))
    assert len(rows) == 1
    assert rows[0].site_price == Decimal("120")


# --- Критерии ------------------------------------------------------------


async def test_identical_criterion_increments_counter(session: AsyncSession) -> None:
    first_id, created_first = await repo.upsert_criterion(
        session, text_value="Срок поставки важнее цены", direction="plus", weight=1.0
    )
    second_id, created_second = await repo.upsert_criterion(
        session, text_value="  срок  поставки   важнее цены  ", direction="plus", weight=1.5
    )
    await session.commit()

    assert first_id == second_id
    assert created_first is True and created_second is False
    rows = await repo.list_criteria(session)
    assert rows[0].times_seen == 2


async def test_different_criteria_stay_separate(session: AsyncSession) -> None:
    first, _ = await repo.upsert_criterion(
        session, text_value="Срок поставки важнее цены", direction="plus", weight=1.0
    )
    second, _ = await repo.upsert_criterion(
        session, text_value="Без действующего РУ не рассматриваем", direction="minus", weight=2.0
    )
    await session.commit()
    assert first != second


async def test_same_as_hint_from_model_is_respected(session: AsyncSession) -> None:
    known, _ = await repo.upsert_criterion(
        session, text_value="Срок важнее цены", direction="plus", weight=1.0
    )
    await session.commit()

    same, created = await repo.upsert_criterion(
        session,
        text_value="Лучше подождать, чем переплатить",
        direction="plus",
        weight=1.0,
        same_as=known,
    )
    await session.commit()
    assert same == known
    assert created is False


# --- Расходы -------------------------------------------------------------


async def test_spent_today_sums_only_today(session: AsyncSession) -> None:
    from decimal import Decimal

    for cost in ("0.004", "0.002"):
        await repo.record_api_call(
            session,
            service="perplexity",
            operation="search",
            request_id=None,
            tokens_in=None,
            tokens_out=None,
            cost_usd=Decimal(cost),
            status="ok",
            duration_ms=100,
        )
    await session.commit()

    assert await repo.spent_today(session) == Decimal("0.006")
    stats = await repo.stats_today(session)
    assert stats[0].service == "perplexity"
    assert stats[0].calls == 2


# --- Кэш реестра ---------------------------------------------------------


async def test_registry_cache_roundtrip_and_expiry(session: AsyncSession) -> None:
    await repo.put_registry_cache(session, "ключ", "found", {"records": []})
    await session.commit()

    assert await repo.get_registry_cache(session, "ключ", 30) is not None
    # Возраст 0 дней — любая запись уже протухла.
    assert await repo.get_registry_cache(session, "ключ", 0) is None


async def test_request_status_transitions(session: AsyncSession) -> None:
    request = await repo.create_request(session, product="Т", raw_input="", input_kind="text")
    assert request.status == RequestStatus.SEARCH

    await repo.set_request_status(session, int(request.id), RequestStatus.AWAITING_CHOICE)
    await session.commit()

    active = await repo.get_active_request(session)
    assert active is not None and active.status == RequestStatus.AWAITING_CHOICE

    await repo.set_request_status(session, int(request.id), RequestStatus.CLOSED)
    await session.commit()
    assert await repo.get_active_request(session) is None
