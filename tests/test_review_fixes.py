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


# --- №3: чужой текст в Telegram-HTML ----------------------------------------


def test_for_owner_escapes_html_so_telegram_accepts_the_reply() -> None:
    """``Иван <ivan@x.ru> писал(а):`` есть почти в каждом ответе. Без
    экранирования Telegram отвергал сообщение, а письмо уже было помечено
    обработанным — и терялось."""
    from bot.services import guard

    framed = guard.for_owner("Иван <ivan@x.ru> писал(а): цена A&D — 100")
    assert "&lt;ivan@x.ru&gt;" in framed
    assert "A&amp;D" in framed
    assert "<ivan@x.ru>" not in framed
    assert texts.UNTRUSTED_OPEN in framed and texts.UNTRUSTED_CLOSE in framed


def test_supplier_names_are_escaped_in_every_owner_facing_text() -> None:
    nasty = "ООО <Медтех> & Ко"
    for rendered in (
        texts.reply_received(nasty, "RFQ-2026-001"),
        texts.selection_confirmed(nasty),
        texts.mail_already_sent(nasty),
        texts.mail_draft(nasty, "a@b.ru", "<script>"),
        texts.intake_recognised(nasty, "10 <шт>", "RFQ-2026-001"),
        texts.kp_price_suspicious(nasty, "1 000.00"),
        texts.blacklist_line(1, nasty, "<плохо>", "01.01.2026"),
    ):
        assert "<Медтех>" not in rendered, rendered
        assert (
            "&lt;Медтех&gt;" in rendered or "&lt;шт&gt;" in rendered or "&lt;script&gt;" in rendered
        )


def test_report_render_escapes_third_party_strings() -> None:
    from bot.services.report import Report, render
    from tests.test_report_render import make_view

    view = make_view(
        supplier_name="A&D <Медтех>",
        domain="a&d.ru",
        email="sales@a&d.ru",
        ru_holder="АО <Держатель>",
    )
    view.reason = "дешевле <всех>"
    view.concerns = ["нет РУ & сайта"]
    report = Report(request_token="RFQ-1", product="Тонометр <UA>", candidates=[view])
    text = "\n".join(render(report))
    for raw in ("<Медтех>", "<Держатель>", "<всех>", "<UA>"):
        assert raw not in text
    assert "A&amp;D &lt;Медтех&gt;" in text
    assert "&lt;UA&gt;" in text


# --- №3: курсор почты двигается после обработки ----------------------------


class _FakeBot:
    def __init__(self) -> None:
        self.messages: list[dict[str, Any]] = []

    async def send_message(self, chat_id: int, text: str, **kwargs: Any) -> None:
        self.messages.append({"text": text, **kwargs})

    async def send_document(self, chat_id: int, document: Any, **kwargs: Any) -> None:
        self.messages.append({"document": document})


async def test_poll_batch_retries_a_failed_letter_and_keeps_the_cursor(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from bot import scheduler

    scheduler._handled.clear()
    scheduler._attempts.clear()
    handled: list[str] = []

    async def flaky(bot: Any, message_id: str) -> None:
        handled.append(message_id)
        if message_id == "m2":
            raise RuntimeError("can't parse entities")

    monkeypatch.setattr(scheduler, "_handle_incoming", flaky)
    bot = _FakeBot()

    # Первый цикл: m1 разобрано, m2 упало — курсор стоит.
    assert await scheduler._handle_batch(bot, ["m1", "m2", "m3"]) is False  # type: ignore[arg-type]
    assert handled == ["m1", "m2"]
    # Второй цикл, тот же срез: m1 не повторяется, m2 пробуется снова.
    assert await scheduler._handle_batch(bot, ["m1", "m2", "m3"]) is False  # type: ignore[arg-type]
    assert handled == ["m1", "m2", "m2"]
    # Третья неудача — сдаёмся, владелец предупреждён, срез дочитан.
    assert await scheduler._handle_batch(bot, ["m1", "m2", "m3"]) is True  # type: ignore[arg-type]
    assert handled == ["m1", "m2", "m2", "m2", "m3"]
    assert any("m2" in m["text"] for m in bot.messages)
    scheduler._handled.clear()
    scheduler._attempts.clear()


class _FakeMail:
    def __init__(self, headers: Any) -> None:
        self.headers = headers
        self.fetches: list[bool] = []

    async def get_message(self, message_id: str, *, full: bool = True, **kwargs: Any) -> Any:
        self.fetches.append(full)
        return self.headers

    async def download_attachment(self, *args: Any, **kwargs: Any) -> bytes | None:
        return None


@needs_db
async def test_incoming_reply_is_shown_escaped_and_recorded_once(
    db: async_sessionmaker[AsyncSession], monkeypatch: pytest.MonkeyPatch
) -> None:
    """Стык почта → база → Telegram: ответ привязывается по Message-ID,
    показывается экранированным, пишется один раз даже при повторе среза."""
    from bot import scheduler
    from bot.db.repo import SupplierInput
    from bot.services.mail import ReplyHeaders

    async with db() as session:
        request = await repo.create_request(session, product="Т", raw_input="", input_kind="text")
        ids = await repo.upsert_suppliers(
            session, [SupplierInput(name="ООО Медтех", domain="m.ru", email="s@m.ru")]
        )
        quote = await repo.reserve_quote(
            session, request_id=int(request.id), supplier_id=ids["m.ru"], message_id="<our@m>"
        )
        assert quote is not None
        await repo.mark_quote_sent(session, int(quote.id), gmail_thread="t1")
        await session.commit()
        quote_id = int(quote.id)

    body = "Иван <ivan@m.ru> писал(а): цена 100"
    headers = ReplyHeaders(
        gmail_id="g1",
        thread_id="t1",
        subject="Re: [RFQ] x",
        from_email="s@m.ru",
        message_ids=["<our@m>"],
        body=body,
    )
    mail = _FakeMail(headers)
    monkeypatch.setattr(scheduler, "get_mail_service", lambda: mail)
    offered: list[int] = []

    async def fake_offer(bot: Any, chat_id: int, *, quote_id: int, **kwargs: Any) -> None:
        offered.append(quote_id)

    monkeypatch.setattr(scheduler, "offer_kp", fake_offer)
    bot = _FakeBot()

    await scheduler._handle_incoming(bot, "g1")  # type: ignore[arg-type]

    assert mail.fetches == [False, True], "сначала заголовки, тело — только для привязанного"
    shown = [m for m in bot.messages if "&lt;ivan@m.ru&gt;" in m["text"]]
    assert shown and shown[0]["parse_mode"] == "HTML"
    assert offered == [quote_id]
    async with db() as session:
        saved = await repo.get_quote(session, quote_id)
        assert saved is not None and saved.replied_at is not None and saved.reply_text == body

    # Повтор среза после сбоя на соседнем письме: второй раз не показываем.
    before = len(bot.messages)
    await scheduler._handle_incoming(bot, "g1")  # type: ignore[arg-type]
    assert len(bot.messages) == before
    assert offered == [quote_id]


# --- №7: пара занимается до отправки ---------------------------------------


@needs_db
async def test_quote_pair_is_reserved_before_send_and_released_on_failure(
    db: async_sessionmaker[AsyncSession],
) -> None:
    import datetime as dt

    from bot.db.models import QuoteRequest, QuoteStatus
    from bot.db.repo import SupplierInput

    async with db() as session:
        request = await repo.create_request(session, product="Т", raw_input="", input_kind="text")
        ids = await repo.upsert_suppliers(session, [SupplierInput(name="X", domain="x.ru")])
        rid, sid = int(request.id), ids["x.ru"]

        first = await repo.reserve_quote(session, request_id=rid, supplier_id=sid, message_id="<1>")
        assert first is not None and first.status == QuoteStatus.SENDING
        # Второе одобрение на ту же пару, пока первое ещё отправляется.
        assert (
            await repo.reserve_quote(session, request_id=rid, supplier_id=sid, message_id="<2>")
            is None
        )

        # Отправка не удалась — пару можно занять снова, Message-ID новый.
        await repo.mark_quote_failed(session, int(first.id))
        again = await repo.reserve_quote(session, request_id=rid, supplier_id=sid, message_id="<3>")
        assert again is not None and again.id == first.id and again.message_id == "<3>"

        # Отправлено — больше никогда.
        await repo.mark_quote_sent(session, int(again.id), gmail_thread="t")
        assert (
            await repo.reserve_quote(session, request_id=rid, supplier_id=sid, message_id="<4>")
            is None
        )

        # Зависшее sending старше порога считается брошенным.
        stale = dt.datetime.now(dt.UTC) - dt.timedelta(minutes=repo.QUOTE_SENDING_STALE_MINUTES + 1)
        row = await session.get(QuoteRequest, int(again.id))
        assert row is not None
        row.status, row.sent_at = QuoteStatus.SENDING, stale
        await session.flush()
        assert (
            await repo.reserve_quote(session, request_id=rid, supplier_id=sid, message_id="<5>")
            is not None
        )


# --- №15, №21: пересылка с Message-ID, заголовки до тела ---------------------


class _RecordingClient:
    def __init__(self, results: list[CallResult]) -> None:
        self.results = results
        self.calls: list[dict[str, Any]] = []

    async def post(self, url: str, **kwargs: Any) -> CallResult:
        self.calls.append({"method": "POST", "url": url, **kwargs})
        return self.results.pop(0)

    async def get(self, url: str, **kwargs: Any) -> CallResult:
        self.calls.append({"method": "GET", "url": url, **kwargs})
        return self.results.pop(0)

    async def aclose(self) -> None:
        return None


@pytest.fixture
def mail_service(monkeypatch: pytest.MonkeyPatch) -> Any:
    import datetime as dt

    from bot.config import get_settings
    from bot.services.mail import MailService

    settings = get_settings()
    for name, value in (
        ("google_client_id", "id"),
        ("google_client_secret", "secret"),
        ("google_refresh_token", "1//r"),
        ("gmail_sender", "bot@example.com"),
    ):
        monkeypatch.setattr(settings, name, value)

    def _make(results: list[CallResult]) -> tuple[MailService, _RecordingClient]:
        service = MailService()
        client = _RecordingClient(results)
        service._client = client  # type: ignore[assignment]
        service._access_token = "t"
        service._expires_at = dt.datetime.now(dt.UTC) + dt.timedelta(hours=1)
        return service, client

    return _make


async def test_forward_failure_checks_the_mailbox_before_giving_up(mail_service: Any) -> None:
    service, client = mail_service(
        [
            CallResult(ok=False, status_code=504, error="таймаут"),
            CallResult(ok=True, status_code=200, json={"messages": [{"id": "m1"}]}),
            CallResult(ok=True, status_code=200, json={"threadId": "t"}),
        ]
    )
    await service.forward_file(
        to="me@x.ru", filename="f.pdf", content=b"%PDF", mime_type="application/pdf"
    )
    assert sum(1 for c in client.calls if c["url"].endswith("/messages/send")) == 1
    assert "rfc822msgid:" in client.calls[1]["params"]["q"]


async def test_send_uses_the_message_id_reserved_in_the_database(mail_service: Any) -> None:
    service, _ = mail_service([CallResult(ok=True, status_code=200, json={"threadId": "t"})])
    _, message_id = await service.send(
        to="a@b.ru", token="RFQ-2026-001", subject_suffix="s", body="b", message_id="<reserved@x>"
    )
    assert message_id == "<reserved@x>"


async def test_headers_are_fetched_as_metadata_and_body_as_full(mail_service: Any) -> None:
    service, client = mail_service([CallResult(ok=True, status_code=200, json={"id": "g"})] * 2)
    await service.get_message("g", full=False)
    await service.get_message("g", full=True)
    assert client.calls[0]["params"]["format"] == "metadata"
    assert "From" in client.calls[0]["params"]["metadataHeaders"]
    assert client.calls[1]["params"]["format"] == "full"


# --- №4: потолок на заявку растёт в процессе, Gemini — одной строкой -------


def _json_transport(payload: dict[str, Any], *, base_url: str = "") -> Any:
    import httpx

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json=payload)

    return httpx.AsyncClient(transport=httpx.MockTransport(handler), base_url=base_url)


@pytest.fixture
def small_cap(monkeypatch: pytest.MonkeyPatch) -> None:
    from bot.config import get_settings
    from bot.services import budget

    budget.reset()
    monkeypatch.setattr(get_settings(), "max_cost_per_request_usd", 0.01)

    async def _zero(request_id: int) -> Any:
        from decimal import Decimal

        return Decimal(0)

    monkeypatch.setattr(budget, "_seed", _zero)
    yield
    budget.reset()


async def test_cap_accumulates_through_the_api_client(small_cap: None) -> None:
    """Раньше budget.record не вызывался нигде: счётчик засевался из базы один
    раз и в памяти не рос, и потолок срабатывал только после перезапуска."""
    from decimal import Decimal

    from bot.services.http import ApiClient

    client = ApiClient("firecrawl")
    client._client = _json_transport({"ok": True})

    first = await client.post("https://x/scrape", request_id=1, cost_usd=Decimal("0.006"))
    second = await client.post("https://x/scrape", request_id=1, cost_usd=Decimal("0.006"))
    assert first.ok is True
    assert second.ok is False and second.budget_exceeded is True
    await client.aclose()


async def test_gemini_call_is_one_row_with_cost_and_cached_tokens(
    monkeypatch: pytest.MonkeyPatch, metered: list[dict[str, Any]]
) -> None:
    """Цена по токенам считается через price-callback и ложится в ту же строку,
    что и вызов; cached_tokens доезжает до учёта; второй строки «:tokens» нет."""
    from bot.config import get_settings
    from bot.services.gemini import API_BASE, GeminiService, Part

    monkeypatch.setattr(get_settings(), "gemini_api_key", "k")
    service = GeminiService()
    service._client._client = _json_transport(
        base_url=API_BASE,
        payload={
            "candidates": [{"content": {"parts": [{"text": '{"product": "Тонометр"}'}]}}],
            "usageMetadata": {
                "promptTokenCount": 1000,
                "candidatesTokenCount": 100,
                "cachedContentTokenCount": 400,
            },
        },
    )
    parsed = await service.generate_json(
        parts=[Part(text="x")], system_instruction="y", request_id=5, operation="intake.text"
    )
    assert parsed == {"product": "Тонометр"}
    rows = [m for m in metered if m["service"] == "gemini"]
    assert len(rows) == 1
    assert rows[0]["cost_usd"] and rows[0]["cost_usd"] > 0
    assert rows[0]["cached_tokens"] == 400
    assert rows[0]["tokens_in"] == 1000 and rows[0]["tokens_out"] == 100
    await service.aclose()


async def test_model_call_is_refused_once_the_cap_is_reached(small_cap: None) -> None:
    """Вызов модели не имеет цены до ответа — раньше он шёл мимо потолка."""
    from decimal import Decimal

    from bot.services import budget
    from bot.services.http import ApiClient, Usage

    budget.record(1, Decimal("0.01"))
    client = ApiClient("gemini")
    client._client = _json_transport({})
    result = await client.post("https://x/gen", request_id=1, price=lambda p: Usage())
    assert result.ok is False and result.budget_exceeded is True
    # Бесплатный вызов (реестр, Gmail) потолок не трогает.
    free = await client.post("https://x/free", request_id=1)
    assert free.ok is True
    await client.aclose()


@needs_db
async def test_intake_spend_is_attached_to_the_request_it_produced(
    db: async_sessionmaker[AsyncSession],
) -> None:
    from decimal import Decimal

    async with db() as session:
        await repo.record_api_call(
            session,
            service="gemini",
            operation="intake.photo:gemini-flash-latest",
            request_id=None,
            tokens_in=10,
            tokens_out=5,
            cost_usd=Decimal("0.02"),
            status="ok",
            duration_ms=1,
        )
        await session.commit()
        request = await repo.create_request(session, product="Т", raw_input="", input_kind="photo")
        attached = await repo.attach_orphan_api_calls(
            session, int(request.id), operation_prefix="intake."
        )
        await session.commit()
        assert attached == 1
        assert await repo.spent_on_request(session, int(request.id)) == Decimal("0.02")


# --- №6: упавший поиск — не «ничего не нашёл» --------------------------------


class _FakeMessage:
    def __init__(self) -> None:
        self.sent: list[str] = []

    async def answer(self, text: str, **kwargs: Any) -> None:
        self.sent.append(text)


@needs_db
async def test_failed_search_is_reported_as_a_failure(
    db: async_sessionmaker[AsyncSession], monkeypatch: pytest.MonkeyPatch
) -> None:
    from bot.config import get_settings
    from bot.db.models import RequestStatus
    from bot.handlers import intake
    from bot.pipeline import SearchSummary
    from bot.services.gemini import ProductRequest

    monkeypatch.setattr(get_settings(), "perplexity_api_key", "k")

    async def failing(**kwargs: Any) -> SearchSummary:
        return SearchSummary(search_failed=True, errors=["HTTP 503: сервер лёг"])

    monkeypatch.setattr(intake, "run_search", failing)
    message = _FakeMessage()
    await intake._start_pipeline(message, ProductRequest(product="Тонометр", raw_input="т"), "text")  # type: ignore[arg-type]

    assert texts.SEARCH_NOTHING not in message.sent
    assert any("Поиск не отработал" in text and "503" in text for text in message.sent)
    async with db() as session:
        request = await repo.get_active_request(session)
        assert request is None, "заявка закрыта, а не висит в search"
        closed = (
            await session.execute(__import__("sqlalchemy").text("select status from requests"))
        ).scalar()
        assert closed == RequestStatus.CLOSED


# --- №17: статус меняется только по таблице переходов -----------------------


@needs_db
async def test_cancel_during_report_is_not_overwritten(
    db: async_sessionmaker[AsyncSession],
) -> None:
    """/cancel пришёл, пока собирался отчёт. Раньше build_report безусловно
    писал awaiting_choice поверх closed, и закрытая заявка снова ждала выбора."""
    from bot.db.models import RequestStatus

    async with db() as session:
        request = await repo.create_request(session, product="Т", raw_input="", input_kind="text")
        rid = int(request.id)
        assert await repo.transition(session, rid, RequestStatus.REPORT) is True
        assert await repo.transition(session, rid, RequestStatus.CLOSED) is True
        # Конвейер доходит до конца уже после отмены.
        assert await repo.transition(session, rid, RequestStatus.AWAITING_CHOICE) is False
        await session.commit()
        fresh = await repo.get_request(session, rid)
        assert fresh is not None and fresh.status == RequestStatus.CLOSED
        assert await repo.list_requests_awaiting_choice(session) == []


@needs_db
async def test_transitions_follow_the_table(db: async_sessionmaker[AsyncSession]) -> None:
    from bot.db.models import RequestStatus

    async with db() as session:
        request = await repo.create_request(session, product="Т", raw_input="", input_kind="text")
        rid = int(request.id)
        # Из search сразу в awaiting_reply нельзя.
        assert await repo.transition(session, rid, RequestStatus.AWAITING_REPLY) is False
        for step in (
            RequestStatus.REPORT,
            RequestStatus.AWAITING_CHOICE,
            RequestStatus.AWAITING_REPLY,
            RequestStatus.KP,
            RequestStatus.KP,  # второй ответ того же поставщика
            RequestStatus.CLOSED,
        ):
            assert await repo.transition(session, rid, step) is True, step
        with pytest.raises(ValueError):
            await repo.transition(session, rid, "mail_sent")


# --- №13: антифлуд не глотает нажатия кнопок -------------------------------


async def test_throttle_lets_callbacks_through_and_collapses_albums() -> None:
    from types import SimpleNamespace

    from aiogram.types import CallbackQuery, Message

    from bot.middleware import ThrottleMiddleware

    seen: list[str] = []

    async def handler(event: Any, data: dict[str, Any]) -> str:
        seen.append(type(event).__name__)
        return "ok"

    throttle = ThrottleMiddleware(interval=60.0)
    user = SimpleNamespace(id=42)
    message = Message.model_construct(message_id=1, media_group_id=None)
    callback = CallbackQuery.model_construct(id="c1")

    assert await throttle(handler, message, {"event_from_user": user}) == "ok"
    # Тап «Да» через полсекунды после сообщения — доходит.
    assert await throttle(handler, callback, {"event_from_user": user}) == "ok"
    # Второе сообщение внутри интервала — отбрасывается, как и раньше.
    assert await throttle(handler, message, {"event_from_user": user}) is None

    album = ThrottleMiddleware(interval=0.0)
    first = Message.model_construct(message_id=2, media_group_id="alb")
    second = Message.model_construct(message_id=3, media_group_id="alb")
    assert await album(handler, first, {"event_from_user": user}) == "ok"
    assert await album(handler, second, {"event_from_user": user}) is None


# --- №10: один нормализатор домена ------------------------------------------


def test_search_dedupes_by_the_same_key_the_database_uses() -> None:
    from bot.services.domains import normalise_domain
    from bot.services.perplexity import _parse_suppliers, domain_of

    assert domain_of("https://medtech.ru#contacts") == normalise_domain("https://medtech.ru/")
    assert domain_of("medtech.ru.") == "medtech.ru"
    suppliers = _parse_suppliers(
        '{"suppliers": [{"name": "A", "site": "https://medtech.ru/"}, '
        '{"name": "B", "site": "https://medtech.ru#contacts"}]}',
        citations=["https://www.medtech.ru."],
    )
    assert [s.site for s in suppliers] == ["https://medtech.ru/"]


@needs_db
async def test_upsert_survives_duplicate_domains_in_one_batch(
    db: async_sessionmaker[AsyncSession],
) -> None:
    """Раньше два входа с одним lower(domain) в одном пакете роняли весь
    поиск: «ON CONFLICT DO UPDATE command cannot affect row a second time»."""
    from bot.db.repo import SupplierInput

    async with db() as session:
        ids = await repo.upsert_suppliers(
            session,
            [
                SupplierInput(name="ООО Медтех", domain="https://medtech.ru/", email=None),
                SupplierInput(name="medtech.ru", domain="medtech.ru#top", email="s@medtech.ru"),
            ],
        )
        await session.commit()
        assert list(ids) == ["medtech.ru"]
        supplier = await repo.get_supplier(session, ids["medtech.ru"])
        assert supplier is not None
        assert supplier.name == "ООО Медтех", "первое имя остаётся"
        assert supplier.email == "s@medtech.ru", "пустое поле дополняется вторым входом"


# --- №19, №22: классификатор вне event loop, pg_trgm один раз ---------------


async def test_async_screening_runs_the_classifier_in_another_thread(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import threading

    from bot.config import get_settings
    from bot.services import guard

    main_thread = threading.get_ident()
    seen: list[int] = []

    def fake_score(text: str) -> float:
        seen.append(threading.get_ident())
        return 0.95

    monkeypatch.setattr(get_settings(), "prompt_guard_enabled", True)
    monkeypatch.setattr(guard, "_load_prompt_guard", lambda: True)
    monkeypatch.setattr(guard, "_model_score", fake_score)

    result = await guard.screen_third_party_async("обычный текст страницы", source="сайт")
    assert result.suspicious is True
    assert seen and seen[0] != main_thread, "torch считает не в потоке event loop"


@needs_db
async def test_trigram_check_is_cached_and_events_are_batched(
    db: async_sessionmaker[AsyncSession],
) -> None:
    from sqlalchemy import func, select

    from bot.db.models import CriterionEvent
    from bot.services.criteria import ExtractedCriterion, SelectionOutcome, persist

    repo.reset_trigram_cache()
    async with db() as session:
        assert await repo._trigram_available(session) is True
        assert repo._trigram_cache is True

        # Дальше запрос к pg_extension не нужен: подменяем scalar и убеждаемся,
        # что кэш отвечает сам.
        async def boom(*args: Any, **kwargs: Any) -> Any:
            raise AssertionError("pg_extension спросили второй раз")

        original = session.scalar
        session.scalar = boom  # type: ignore[method-assign]
        assert await repo._trigram_available(session) is True
        session.scalar = original  # type: ignore[method-assign]

        request = await repo.create_request(session, product="Т", raw_input="", input_kind="text")
        outcome = SelectionOutcome(
            criteria=[
                ExtractedCriterion(text="срок важнее цены", direction="plus", weight=1.0),
                ExtractedCriterion(text="без РУ не берём", direction="minus", weight=2.0),
            ],
            transcript="беру второго",
        )
        total, created = await persist(session, outcome, request_id=int(request.id))
        await session.commit()
        assert (total, created) == (2, 2)
        count = await session.scalar(select(func.count()).select_from(CriterionEvent))
        assert count == 2


# --- №24–28: конвенции проекта ---------------------------------------------


def test_every_system_instruction_lives_in_prompts_dir() -> None:
    """Правило проекта: инструкции в prompts/*.md, не в коде. Раньше три из
    них были строками в gemini.py и kp.py, а четвёртая — в perplexity.py."""
    from pathlib import Path

    from bot.config import get_settings

    prompts = Path(get_settings().prompts_dir)
    for name in ("product", "transcribe", "extract", "search", "report", "email", "criteria"):
        assert (prompts / f"{name}.md").read_text(encoding="utf-8").strip(), name

    for module in ("gemini", "kp", "perplexity"):
        source = Path(f"bot/services/{module}.py").read_text(encoding="utf-8")
        assert "_INSTRUCTION = (" not in source, f"в {module}.py осталась инструкция строкой"


def test_handlers_keep_no_user_facing_strings() -> None:
    """Все тексты для владельца — в bot/texts.py."""
    import re
    from pathlib import Path

    cyrillic_literal = re.compile(r"answer\(\s*f?\"[^\"]*[А-Яа-яЁё]")
    for path in Path("bot/handlers").glob("*.py"):
        source = path.read_text(encoding="utf-8")
        assert not cyrillic_literal.search(source), f"{path}: текст владельцу мимо texts.py"
    assert not re.search(r"send_message\([^)]*\"[^\"]*[А-Яа-яЁё]", Path("bot/main.py").read_text())


def test_llm_json_parser_handles_fences_and_prose() -> None:
    from bot.services.llm_json import parse_llm_json

    assert parse_llm_json('```json\n{"a": 1}\n```') == {"a": 1}
    assert parse_llm_json('Вот ответ:\n```\n{"a": [1, 2]}\n```\nСпасибо.') == {"a": [1, 2]}
    assert parse_llm_json('{"a": 1}') == {"a": 1}
    assert parse_llm_json("не json") is None


def test_contact_email_rule_is_shared_by_search_and_scrape() -> None:
    from bot.services.contacts import extract_email, is_contact_email
    from bot.services.perplexity import _parse_suppliers

    assert is_contact_email("sales@medtech.ru") is True
    assert is_contact_email("noreply@medtech.ru") is False
    assert is_contact_email("logo@2x.png") is False
    assert extract_email("пишите noreply@x.ru или sales@x.ru") == "sales@x.ru"

    found = _parse_suppliers(
        '{"suppliers": [{"name": "A", "site": "https://a.ru", "email": "noreply@a.ru"}]}', []
    )
    assert found[0].email == "", "noreply из выдачи поиска больше не становится контактом"


async def test_prompt_payloads_are_wrapped_by_default(monkeypatch: pytest.MonkeyPatch) -> None:
    """Флаг untrusted нельзя забыть: рамка по умолчанию."""
    from bot.config import get_settings
    from bot.services import guard
    from bot.services.gemini import GeminiService

    monkeypatch.setattr(get_settings(), "gemini_api_key", "k")
    service = GeminiService()
    seen: dict[str, Any] = {}

    async def spy(**kwargs: Any) -> dict[str, Any]:
        seen.update(kwargs)
        return {}

    monkeypatch.setattr(service, "generate_json", spy)
    await service.run_prompt_file("email", {"supplier": {"name": "ООО X"}})
    text = seen["parts"][0].text
    assert guard.wrap_untrusted("", source="x").split("\n")[0].split("(")[0] in text
    assert '"ООО X"' in text


def test_dead_helpers_are_gone() -> None:
    from bot.db import repo
    from bot.services.http import CallResult
    from bot.services.registry import RegistryResult

    for name in (
        "get_request_by_token",
        "find_supplier_by_email",
        "get_candidate",
        "list_silent_quotes",
        "list_orders_for_supplier",
        "set_request_status",
    ):
        assert not hasattr(repo, name), name
    assert not hasattr(CallResult, "timed_out")
    assert not hasattr(RegistryResult, "found")
    assert not hasattr(texts, "KP_FINAL_READY")
