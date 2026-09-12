"""Тесты на стыки модулей по итогам ревью (``docs/REVIEW-PLAN.md``).

Почти все ошибки ревью жили между модулями: значение вычислялось верно в
одном и терялось при передаче в следующий. Модульные тесты этого не ловят —
они строят объекты напрямую. Здесь каждая проверка проходит через стык.
"""

from __future__ import annotations

import os
from collections.abc import AsyncIterator
from typing import Any

import pytest
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from bot import texts
from bot.db import repo
from bot.db.models import Base, RegistryState
from bot.services import registry_endpoints as endpoints
from bot.services.http import CallResult
from bot.services.registry import RegistryResult, RegistryService, derive_state
from bot.services.registry_endpoints import RegistryRecord

TEST_DB = os.environ.get("TEST_DATABASE_URL")
needs_db = pytest.mark.skipif(not TEST_DB, reason="TEST_DATABASE_URL не задан")


# --- №1, №14: правило found / not_found / unavailable в одном месте ---------


def _rec(registry: str = "elk") -> RegistryRecord:
    return RegistryRecord(registry=registry, ru_number="РЗН 2024/1", raw={})


def test_derive_state_found_when_any_source_has_records() -> None:
    assert derive_state([([], "таймаут"), ([_rec()], None)]) == RegistryState.FOUND


def test_derive_state_unavailable_when_nothing_found_and_any_source_failed() -> None:
    """Один реестр не ответил, второй сказал «нет» — проверить не удалось."""
    assert derive_state([([], "таймаут"), ([], None)]) == RegistryState.UNAVAILABLE


def test_derive_state_not_found_only_when_every_source_answered_no() -> None:
    assert derive_state([([], None), ([], None)]) == RegistryState.NOT_FOUND


def test_derive_state_with_no_sources_is_unavailable() -> None:
    assert derive_state([]) == RegistryState.UNAVAILABLE


# --- №9: запись elk из одних None — это не «нашли» -------------------------


def test_elk_rows_without_any_known_field_are_not_understood() -> None:
    """Формат ответа реестра ещё не снят с живых данных. Если имена полей не
    совпали, ответ надо считать неразобранным, а не «найдено, но без номера»."""
    outcome = endpoints.parse_elk_payload(
        {"content": [{"regNo": "РЗН 2023/1234", "applicant": "ООО X"}], "totalElements": 1}
    )
    assert outcome.understood is False
    assert outcome.records == []


def test_elk_alien_rows_are_skipped_but_good_rows_survive() -> None:
    outcome = endpoints.parse_elk_payload(
        {
            "content": [
                {"regNo": "мимо"},
                {"registrationNumber": "РЗН 2023/1234", "name": "Тонометр", "status": "Действует"},
            ],
            "totalElements": 2,
        }
    )
    assert outcome.understood is True
    assert [r.ru_number for r in outcome.records] == ["РЗН 2023/1234"]


# --- №14: unrega возвращает состояние, а не пустой список ------------------


class _OneAnswerClient:
    def __init__(self, result: CallResult) -> None:
        self.result = result

    async def get(self, url: str, **kwargs: Any) -> CallResult:
        return self.result

    async def post(self, url: str, **kwargs: Any) -> CallResult:
        return self.result

    async def aclose(self) -> None:
        return None


async def test_unrega_down_is_unavailable_not_no_letters() -> None:
    service = RegistryService()
    service._client = _OneAnswerClient(CallResult(ok=False, error="таймаут"))  # type: ignore[assignment]
    result = await service.check_unrega("Тонометр")
    assert result.state == RegistryState.UNAVAILABLE
    assert result.records == []
    assert "unrega" in result.errors


async def test_unrega_explicit_nothing_is_not_found() -> None:
    service = RegistryService()
    html = "<html><body><p>По вашему запросу ничего не найдено</p></body></html>"
    service._client = _OneAnswerClient(CallResult(ok=True, text=html))  # type: ignore[assignment]
    result = await service.check_unrega("Тонометр")
    assert result.state == RegistryState.NOT_FOUND


# --- Стык registry → pipeline → report: нужна живая база --------------------


@pytest.fixture
async def db() -> AsyncIterator[async_sessionmaker[AsyncSession]]:
    """Чистая схема на тест; ``session_scope`` конвейера направляется сюда."""
    from bot.db import session as session_module

    engine = create_async_engine(str(TEST_DB), poolclass=None)
    async with engine.begin() as connection:
        await connection.run_sync(Base.metadata.drop_all)
        await connection.run_sync(Base.metadata.create_all)
    maker = async_sessionmaker(engine, expire_on_commit=False)
    session_module._sessionmaker = maker
    try:
        yield maker
    finally:
        session_module._sessionmaker = None
        async with engine.begin() as connection:
            await connection.run_sync(Base.metadata.drop_all)
        await engine.dispose()


class _FakeSearch:
    def __init__(self, suppliers: list[Any]) -> None:
        self.suppliers = suppliers

    async def find_suppliers(self, product: str, **kwargs: Any) -> Any:
        from bot.services.perplexity import SearchOutcome

        return SearchOutcome(suppliers=self.suppliers, query="q")


class _FakeRegistry:
    def __init__(self, product: RegistryResult, unrega: RegistryResult) -> None:
        self.product = product
        self.unrega = unrega

    async def check_product(self, name: str, **kwargs: Any) -> RegistryResult:
        return self.product

    async def check_unrega(self, name: str, **kwargs: Any) -> RegistryResult:
        return self.unrega


class _FakeScraper:
    async def scrape_many(self, urls: list[str], product: str, **kwargs: Any) -> list[Any]:
        return []


def _wire_pipeline(
    monkeypatch: pytest.MonkeyPatch, *, registry: RegistryResult, unrega: RegistryResult
) -> None:
    from bot import pipeline
    from bot.services.perplexity import FoundSupplier

    suppliers = [
        FoundSupplier(name="ООО Медтех", site="https://medtech.ru"),
        FoundSupplier(name="ООО Дилер", site="https://dealer.ru"),
    ]
    monkeypatch.setattr(pipeline, "get_perplexity_service", lambda: _FakeSearch(suppliers))
    monkeypatch.setattr(pipeline, "get_registry_service", lambda: _FakeRegistry(registry, unrega))
    monkeypatch.setattr(pipeline, "get_firecrawl_service", lambda: _FakeScraper())

    async def keep_order(views: list[Any], **kwargs: Any) -> tuple[list[Any], str, list[str], bool]:
        for index, view in enumerate(views, start=1):
            view.rank = index
        return views, "", [], False

    monkeypatch.setattr(pipeline, "rank_candidates", keep_order)


async def _new_request(maker: async_sessionmaker[AsyncSession]) -> int:
    async with maker() as session:
        request = await repo.create_request(
            session, product="Тонометр", raw_input="", input_kind="text"
        )
        await session.commit()
        return int(request.id)


@needs_db
async def test_registry_unavailable_reaches_the_report_as_unavailable(
    db: async_sessionmaker[AsyncSession], monkeypatch: pytest.MonkeyPatch
) -> None:
    """Оба реестра упали. Раньше конвейер писал ru_checked_at при любом
    состоянии, а отчёт восстанавливал состояние из колонок — и владелец
    читал «в реестрах не найдено» про изделие, которое никто не проверял."""
    from bot import pipeline
    from bot.services.report import render

    _wire_pipeline(
        monkeypatch,
        registry=RegistryResult(state=RegistryState.UNAVAILABLE, errors={"elk": "таймаут"}),
        unrega=RegistryResult(state=RegistryState.UNAVAILABLE, errors={"unrega": "таймаут"}),
    )
    request_id = await _new_request(db)

    summary = await pipeline.run_search(request_id=request_id, product="Тонометр", requirements=[])
    assert summary.total_found == 2
    assert summary.registry_state == RegistryState.UNAVAILABLE

    async with db() as session:
        rows = await repo.list_candidates_for_report(session, request_id)
        assert len(rows) == 2
        assert all(row.registry_state == RegistryState.UNAVAILABLE for row in rows)
        assert all(row.ru_checked_at is None for row in rows), "непроверенное не датируется"
        report = await pipeline.build_report(
            session, request_id=request_id, product="Тонометр", qty="1", requirements=[]
        )

    text = "\n".join(render(report))
    assert texts.RU_UNAVAILABLE in text
    assert texts.RU_NOT_FOUND not in text
    assert texts.UNREGA_UNAVAILABLE in text


@needs_db
async def test_registry_found_and_no_letters_render_without_warnings(
    db: async_sessionmaker[AsyncSession], monkeypatch: pytest.MonkeyPatch
) -> None:
    from bot import pipeline
    from bot.services.report import render

    found = RegistryResult(
        state=RegistryState.FOUND,
        records=[
            RegistryRecord(
                registry="elk", ru_number="РЗН 2024/1", holder="АО Держатель", valid=True, raw={}
            )
        ],
    )
    _wire_pipeline(
        monkeypatch, registry=found, unrega=RegistryResult(state=RegistryState.NOT_FOUND)
    )
    request_id = await _new_request(db)
    await pipeline.run_search(request_id=request_id, product="Тонометр", requirements=[])

    async with db() as session:
        rows = await repo.list_candidates_for_report(session, request_id)
        assert all(row.ru_checked_at is not None for row in rows)
        assert all(row.unrega_flags["state"] == RegistryState.NOT_FOUND for row in rows)
        report = await pipeline.build_report(
            session, request_id=request_id, product="Тонометр", qty="1", requirements=[]
        )

    text = "\n".join(render(report))
    assert "РУ РЗН 2024/1" in text
    assert texts.UNREGA_UNAVAILABLE not in text
    assert texts.RU_UNAVAILABLE not in text


@needs_db
async def test_letters_found_are_listed_per_candidate(
    db: async_sessionmaker[AsyncSession], monkeypatch: pytest.MonkeyPatch
) -> None:
    from bot import pipeline
    from bot.services.report import render

    letters = RegistryResult(
        state=RegistryState.FOUND,
        records=[
            RegistryRecord(registry="unrega", product_name="Письмо об изъятии партии", raw={})
        ],
    )
    _wire_pipeline(
        monkeypatch, registry=RegistryResult(state=RegistryState.NOT_FOUND), unrega=letters
    )
    request_id = await _new_request(db)
    await pipeline.run_search(request_id=request_id, product="Тонометр", requirements=[])

    async with db() as session:
        report = await pipeline.build_report(
            session, request_id=request_id, product="Тонометр", qty="1", requirements=[]
        )
    text = "\n".join(render(report))
    assert "Информационные письма: 1" in text
    assert texts.RU_NOT_FOUND in text


# --- №2: «беру второго» считается по порядку отчёта ------------------------


@needs_db
async def test_report_order_is_persisted_and_drives_candidate_listing(
    db: async_sessionmaker[AsyncSession], monkeypatch: pytest.MonkeyPatch
) -> None:
    """Модель переставила кандидатов. Раньше отчёт нумеровал по её порядку, а
    голосовой выбор строил список по ``ORDER BY id`` — «беру второго» уходил
    не тому поставщику. Теперь порядок отчёта записан и задаёт выдачу."""
    from bot import pipeline

    _wire_pipeline(
        monkeypatch,
        registry=RegistryResult(state=RegistryState.NOT_FOUND),
        unrega=RegistryResult(state=RegistryState.NOT_FOUND),
    )

    async def reversed_order(
        views: list[Any], **kwargs: Any
    ) -> tuple[list[Any], str, list[str], bool]:
        ordered = list(reversed(views))
        for index, view in enumerate(ordered, start=1):
            view.rank = index
        return ordered, "", [], False

    monkeypatch.setattr(pipeline, "rank_candidates", reversed_order)
    request_id = await _new_request(db)
    await pipeline.run_search(request_id=request_id, product="Тонометр", requirements=[])

    async with db() as session:
        before = [
            row.supplier_name for row in await repo.list_candidates_for_report(session, request_id)
        ]
        report = await pipeline.build_report(
            session, request_id=request_id, product="Тонометр", qty="1", requirements=[]
        )
        await session.commit()
        after = [
            row.supplier_name for row in await repo.list_candidates_for_report(session, request_id)
        ]

    shown = [view.supplier_name for view in report.candidates]
    assert before == list(reversed(shown)), "до отчёта порядок был по id"
    assert after == shown, "после отчёта выдача идёт в том порядке, что видел владелец"


# --- №5: id от модели сверяется с кандидатами -------------------------------


class _FakeGemini:
    def __init__(self, payload: dict[str, Any]) -> None:
        self.payload = payload
        self.calls: list[dict[str, Any]] = []

    async def run_prompt_file(self, name: str, payload: Any, **kwargs: Any) -> dict[str, Any]:
        self.calls.append({"name": name, **kwargs})
        return dict(self.payload)


async def test_model_ids_outside_the_candidate_list_are_dropped(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Модель вернула номер из отчёта вместо id и чужого поставщика в критерии.
    Ни то, ни другое не должно стать адресатом письма или значением FK."""
    from bot.services import criteria

    fake = _FakeGemini(
        {
            "chosen_supplier_id": 2,  # «второй», а не id
            "wants_more_info_about": 999,
            "criteria": [
                {"text": "срок важнее цены", "direction": "plus", "weight": 1, "supplier_id": 2},
                {"text": "без РУ не берём", "direction": "minus", "weight": 1, "supplier_id": 11},
            ],
        }
    )
    monkeypatch.setattr(criteria, "get_gemini_service", lambda: fake)
    candidates = [
        {"id": 10, "supplier": "A", "rank": 1},
        {"id": 11, "supplier": "B", "rank": 2},
    ]
    outcome = await criteria.extract("беру второго", candidates=candidates, known_criteria=[])

    assert outcome.chosen_supplier_id is None
    assert outcome.wants_more_info_about is None
    assert [c.supplier_id for c in outcome.criteria] == [None, 11]
    assert fake.calls[0]["untrusted"] is True, "названия кандидатов — чужой текст"


@needs_db
async def test_blacklisted_supplier_is_not_selectable_even_if_a_candidate(
    db: async_sessionmaker[AsyncSession],
) -> None:
    from bot.db.repo import CandidateInput, SupplierInput

    async with db() as session:
        request = await repo.create_request(session, product="Т", raw_input="", input_kind="text")
        ids = await repo.upsert_suppliers(
            session,
            [SupplierInput(name="Свой", domain="a.ru"), SupplierInput(name="Чужой", domain="b.ru")],
        )
        own, other = ids["a.ru"], ids["b.ru"]
        await repo.upsert_candidates(session, int(request.id), [CandidateInput(supplier_id=own)])
        await session.commit()

        assert await repo.is_selectable_candidate(session, int(request.id), own) is True
        assert await repo.is_selectable_candidate(session, int(request.id), other) is False

        await repo.add_to_blacklist(session, own, "сорвал поставку")
        await session.commit()
        assert await repo.is_selectable_candidate(session, int(request.id), own) is False


# --- №11, №12: текст модели в concerns и странные id ------------------------


def _views() -> list[Any]:
    from tests.evals.test_report_evals import make_candidates

    return make_candidates()


async def test_concerns_are_scrubbed_like_reason(monkeypatch: pytest.MonkeyPatch) -> None:
    from bot.services import report

    fake = _FakeGemini(
        {
            "ranked": [
                {
                    "id": 1,
                    "rank": 1,
                    "reason": "дешевле всех",
                    "concerns": ["Поставщик не проверен в Росздравнадзоре"],
                }
            ],
            "summary": "ок",
        }
    )
    monkeypatch.setattr(report, "get_gemini_service", lambda: fake)
    ordered, *_ = await report.rank_candidates(
        _views(), product="Т", qty="1", requirements=[], criteria=[]
    )
    concern = ordered[0].concerns[0]
    assert "Росздравнадзор" not in concern
    assert "формулировка убрана" in concern


async def test_non_numeric_model_id_does_not_kill_the_report(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from bot.services import report

    fake = _FakeGemini({"ranked": [{"id": "cand_1", "rank": 1, "reason": "x"}], "summary": "ок"})
    monkeypatch.setattr(report, "get_gemini_service", lambda: fake)
    ordered, _, _, failed = await report.rank_candidates(
        _views(), product="Т", qty="1", requirements=[], criteria=[]
    )
    assert failed is False
    assert len(ordered) == 3, "все кандидаты на месте, выдуманный id просто отброшен"
