"""Тесты одобрений: то, что владелец подтверждает кнопкой.

Проверяется главное свойство: **одобрение занимается ровно один раз**. Второе
нажатие, повторная доставка callback от Telegram и кнопка недельной давности
не должны привести ко второй отправке письма поставщику.
"""

from __future__ import annotations

import datetime as dt
import os
from collections.abc import AsyncIterator
from decimal import Decimal

import pytest
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from bot.db import repo
from bot.db.models import ApprovalDecision, ApprovalKind, Base
from bot.db.repo import CandidateInput, SupplierInput

TEST_DB = os.environ.get("TEST_DATABASE_URL")

pytestmark = pytest.mark.skipif(not TEST_DB, reason="TEST_DATABASE_URL не задан")


@pytest.fixture
async def session() -> AsyncIterator[AsyncSession]:
    engine = create_async_engine(str(TEST_DB))
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


async def _request_and_supplier(session: AsyncSession) -> tuple[int, int]:
    request = await repo.create_request(
        session, product="Тонометр", raw_input="", input_kind="text"
    )
    ids = await repo.upsert_suppliers(
        session, [SupplierInput(name="ООО «Медтехника»", domain="medtech.ru")]
    )
    return int(request.id), ids["medtech.ru"]


# --- Одноразовость одобрения ---------------------------------------------


async def test_approval_can_be_claimed_only_once(session: AsyncSession) -> None:
    """Второе нажатие «Да» не должно отправить второе письмо."""
    request_id, supplier_id = await _request_and_supplier(session)
    approval = await repo.create_approval(
        session,
        kind=ApprovalKind.EMAIL,
        request_id=request_id,
        supplier_id=supplier_id,
        payload={"to": "sales@medtech.ru", "body": "текст"},
    )
    await session.commit()

    first = await repo.claim_approval(session, int(approval.id), decision=ApprovalDecision.APPROVED)
    second = await repo.claim_approval(
        session, int(approval.id), decision=ApprovalDecision.APPROVED
    )

    assert first is not None
    assert second is None, "одобрение занялось дважды — письмо ушло бы два раза"


async def test_rejection_also_closes_the_approval(session: AsyncSession) -> None:
    """Нажали «Нет», потом «Да» — второе нажатие уже ничего не делает."""
    request_id, supplier_id = await _request_and_supplier(session)
    approval = await repo.create_approval(
        session,
        kind=ApprovalKind.EMAIL,
        request_id=request_id,
        supplier_id=supplier_id,
        payload={"to": "a@b.ru"},
    )
    await session.commit()

    assert await repo.claim_approval(session, int(approval.id), decision=ApprovalDecision.REJECTED)
    assert (
        await repo.claim_approval(session, int(approval.id), decision=ApprovalDecision.APPROVED)
        is None
    )


async def test_expired_approval_cannot_be_claimed(session: AsyncSession) -> None:
    """Кнопки в Telegram не протухают — протухает одобрение."""
    request_id, supplier_id = await _request_and_supplier(session)
    approval = await repo.create_approval(
        session,
        kind=ApprovalKind.EMAIL,
        request_id=request_id,
        supplier_id=supplier_id,
        payload={"to": "a@b.ru"},
        ttl_hours=0,
    )
    await session.commit()

    assert (
        await repo.claim_approval(session, int(approval.id), decision=ApprovalDecision.APPROVED)
        is None
    )


async def test_unknown_approval_id_is_not_claimable(session: AsyncSession) -> None:
    assert await repo.claim_approval(session, 999_999, decision=ApprovalDecision.APPROVED) is None


# --- Что именно одобрили --------------------------------------------------


async def test_payload_survives_the_roundtrip(session: AsyncSession) -> None:
    """Отправка берёт адресата и текст отсюда, а не перечитывает поставщика."""
    request_id, supplier_id = await _request_and_supplier(session)
    payload = {
        "to": "sales@medtech.ru",
        "token": "RFQ-2026-001",
        "subject_suffix": "Запрос цены — тонометр",
        "body": "Здравствуйте.",
    }
    approval = await repo.create_approval(
        session,
        kind=ApprovalKind.EMAIL,
        request_id=request_id,
        supplier_id=supplier_id,
        payload=payload,
    )
    await session.commit()

    claimed = await repo.claim_approval(
        session, int(approval.id), decision=ApprovalDecision.APPROVED
    )
    assert claimed is not None
    assert claimed.payload == payload


def test_payload_hash_ignores_key_order() -> None:
    assert repo.payload_hash({"a": 1, "b": 2}) == repo.payload_hash({"b": 2, "a": 1})


def test_payload_hash_changes_with_content() -> None:
    """Отпечаток показанного: подменили адресата — хеш другой."""
    assert repo.payload_hash({"to": "a@b.ru"}) != repo.payload_hash({"to": "c@d.ru"})


async def test_applied_result_is_recorded(session: AsyncSession) -> None:
    """В журнале остаётся, чем закончилось одобренное действие."""
    request_id, supplier_id = await _request_and_supplier(session)
    approval = await repo.create_approval(
        session,
        kind=ApprovalKind.EMAIL,
        request_id=request_id,
        supplier_id=supplier_id,
        payload={"to": "a@b.ru"},
    )
    await session.commit()

    await repo.mark_approval_applied(session, int(approval.id), {"message_id": "<x@y>"})
    await session.commit()

    # UPDATE прошёл мимо identity map: сбрасываем её, иначе прочитаем
    # устаревший объект из кэша сессии.
    session.expunge_all()
    stored = await repo.get_approval(session, int(approval.id))
    assert stored is not None
    assert stored.applied_at is not None
    assert stored.result == {"message_id": "<x@y>"}


async def test_stale_approvals_are_marked_expired(session: AsyncSession) -> None:
    request_id, supplier_id = await _request_and_supplier(session)
    await repo.create_approval(
        session,
        kind=ApprovalKind.EMAIL,
        request_id=request_id,
        supplier_id=supplier_id,
        payload={},
        ttl_hours=0,
    )
    await session.commit()

    assert await repo.expire_stale_approvals(session) == 1
    await session.commit()
    # Повторный вызов ничего не находит.
    assert await repo.expire_stale_approvals(session) == 0


# --- Защита от повторной отправки ----------------------------------------


async def test_second_quote_for_same_pair_is_refused(session: AsyncSession) -> None:
    """Уникальность пары «заявка + поставщик» держит база.

    Владелец мог выбрать того же поставщика голосовым во второй раз. Второе
    письмо уходить не должно.
    """
    request_id, supplier_id = await _request_and_supplier(session)
    first = await repo.create_quote_request(
        session,
        request_id=request_id,
        supplier_id=supplier_id,
        gmail_thread="t-1",
        message_id="<a@medisent>",
    )
    await session.commit()
    second = await repo.create_quote_request(
        session,
        request_id=request_id,
        supplier_id=supplier_id,
        gmail_thread="t-2",
        message_id="<b@medisent>",
    )
    await session.commit()

    assert first is not None
    assert second is None, "вторая запись прошла — поставщик получил бы два письма"


async def test_find_quote_sees_the_existing_send(session: AsyncSession) -> None:
    request_id, supplier_id = await _request_and_supplier(session)
    assert await repo.find_quote(session, request_id, supplier_id) is None

    await repo.create_quote_request(
        session,
        request_id=request_id,
        supplier_id=supplier_id,
        gmail_thread="t",
        message_id="<m>",
    )
    await session.commit()
    assert await repo.find_quote(session, request_id, supplier_id) is not None


async def test_same_supplier_in_another_request_is_allowed(session: AsyncSession) -> None:
    """Ограничение на пару, а не на поставщика: по новой заявке писать можно."""
    first_request, supplier_id = await _request_and_supplier(session)
    second = await repo.create_request(
        session, product="Термометр", raw_input="", input_kind="text"
    )
    await repo.create_quote_request(
        session,
        request_id=first_request,
        supplier_id=supplier_id,
        gmail_thread="t1",
        message_id="<a>",
    )
    row = await repo.create_quote_request(
        session,
        request_id=int(second.id),
        supplier_id=supplier_id,
        gmail_thread="t2",
        message_id="<b>",
    )
    await session.commit()
    assert row is not None


# --- Разбор цен один раз на запрос ---------------------------------------


async def test_kp_approval_is_found_for_repeat_letters(session: AsyncSession) -> None:
    """Второе письмо от того же поставщика не должно снова гонять модель."""
    request_id, supplier_id = await _request_and_supplier(session)
    quote = await repo.create_quote_request(
        session,
        request_id=request_id,
        supplier_id=supplier_id,
        gmail_thread="t",
        message_id="<m>",
    )
    assert quote is not None
    await session.commit()

    assert await repo.find_kp_approval(session, int(quote.id)) is None

    await repo.create_approval(
        session,
        kind=ApprovalKind.KP,
        request_id=request_id,
        quote_id=int(quote.id),
        payload={"items": []},
    )
    await session.commit()
    assert await repo.find_kp_approval(session, int(quote.id)) is not None


# --- Привязка голосового к заявке ----------------------------------------


async def test_awaiting_choice_returns_only_waiting_requests(session: AsyncSession) -> None:
    from bot.db.models import RequestStatus

    first = await repo.create_request(session, product="A", raw_input="", input_kind="text")
    second = await repo.create_request(session, product="B", raw_input="", input_kind="text")
    await repo.set_request_status(session, int(first.id), RequestStatus.AWAITING_CHOICE)
    await session.commit()

    waiting = await repo.list_requests_awaiting_choice(session)
    assert [r.id for r in waiting] == [first.id]

    await repo.set_request_status(session, int(second.id), RequestStatus.AWAITING_CHOICE)
    await session.commit()
    waiting = await repo.list_requests_awaiting_choice(session)
    assert len(waiting) == 2, "две заявки ждут выбора — угадывать нельзя, надо спросить"
    # Порядок по времени создания: старая первой.
    assert waiting[0].id == first.id


# --- Расходы по заявке ----------------------------------------------------


async def test_spent_on_request_counts_only_that_request(session: AsyncSession) -> None:
    first = await repo.create_request(session, product="A", raw_input="", input_kind="text")
    second = await repo.create_request(session, product="B", raw_input="", input_kind="text")
    for request_id, cost in (
        (int(first.id), "0.01"),
        (int(first.id), "0.02"),
        (int(second.id), "0.50"),
    ):
        await repo.record_api_call(
            session,
            service="perplexity",
            operation="search",
            request_id=request_id,
            tokens_in=None,
            tokens_out=None,
            cost_usd=Decimal(cost),
            status="ok",
            duration_ms=1,
        )
    await session.commit()

    assert await repo.spent_on_request(session, int(first.id)) == Decimal("0.03")
    assert await repo.spent_on_request(session, int(second.id)) == Decimal("0.50")


async def test_cached_tokens_are_stored(session: AsyncSession) -> None:
    """Кэш-телеметрия: без неё не видно, работает ли стабильный префикс промпта."""
    request = await repo.create_request(session, product="A", raw_input="", input_kind="text")
    await repo.record_api_call(
        session,
        service="gemini",
        operation="report.rank",
        request_id=int(request.id),
        tokens_in=5000,
        tokens_out=300,
        cached_tokens=4200,
        cost_usd=Decimal("0.001"),
        status="ok",
        duration_ms=900,
    )
    await session.commit()

    rows = await repo.stats_today(session)
    assert rows[0].service == "gemini"


# --- Флаг инъекции доезжает до отчёта ------------------------------------


async def test_injection_flag_reaches_the_report_query(session: AsyncSession) -> None:
    """Детектор без последствий бесполезен: флаг должен доехать до выборки."""
    request_id, supplier_id = await _request_and_supplier(session)
    await repo.upsert_candidates(
        session,
        request_id,
        [
            CandidateInput(
                supplier_id=supplier_id,
                raw={"scrape": {"ok": True, "injection_suspected": True}},
            )
        ],
    )
    await session.commit()

    rows = await repo.list_candidates_for_report(session, request_id)
    assert len(rows) == 1
    assert rows[0].injection_suspected is True


async def test_missing_flag_reads_as_false(session: AsyncSession) -> None:
    request_id, supplier_id = await _request_and_supplier(session)
    await repo.upsert_candidates(session, request_id, [CandidateInput(supplier_id=supplier_id)])
    await session.commit()

    rows = await repo.list_candidates_for_report(session, request_id)
    assert not rows[0].injection_suspected


def test_approval_ttl_is_a_day_by_default() -> None:
    """Сутки — компромисс: за это время владелец успевает ответить, а кнопка
    недельной давности уже не стреляет."""
    assert repo.APPROVAL_TTL_HOURS == 24
    assert dt.timedelta(hours=repo.APPROVAL_TTL_HOURS) == dt.timedelta(days=1)
