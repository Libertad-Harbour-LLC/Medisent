"""Тесты матчинга ответных писем.

Второе место, где ТЗ прямо требует тесты: ошибка здесь тихая и дорогая —
ответ привяжется не к той заявке, и заметить это будет некому.

Порядок матчинга проверяется целиком: каждый шаг срабатывает, только когда
предыдущие не сработали, и адрес отправителя стоит последним.
"""

from __future__ import annotations

import base64
from typing import Any

import pytest

from bot.services.mail import (
    ReplyHeaders,
    build_message,
    extract_token,
    match_quote,
    parse_message,
    parse_message_ids,
)

# --- Заглушка репозитория ------------------------------------------------


class FakeQuote:
    def __init__(self, quote_id: int, label: str) -> None:
        self.id = quote_id
        self.label = label

    def __repr__(self) -> str:
        return f"<Quote {self.label}>"


class FakeRepo:
    """Считает, какие шаги матчинга были испробованы и в каком порядке."""

    def __init__(
        self,
        *,
        by_message_id: FakeQuote | None = None,
        by_thread: FakeQuote | None = None,
        by_token: FakeQuote | None = None,
        by_sender: FakeQuote | None = None,
    ) -> None:
        self.by_message_id = by_message_id
        self.by_thread = by_thread
        self.by_token = by_token
        self.by_sender = by_sender
        self.tried: list[str] = []

    async def find_quote_by_message_id(self, session: Any, ids: list[str]) -> FakeQuote | None:
        self.tried.append("message_id")
        return self.by_message_id

    async def find_quote_by_thread(self, session: Any, thread: str) -> FakeQuote | None:
        self.tried.append("thread")
        return self.by_thread

    async def find_quote_by_token(self, session: Any, token: str) -> FakeQuote | None:
        self.tried.append("token")
        return self.by_token

    async def find_quote_by_sender(self, session: Any, email: str) -> FakeQuote | None:
        self.tried.append("sender")
        return self.by_sender


@pytest.fixture
def patch_repo(monkeypatch: pytest.MonkeyPatch) -> Any:
    def _apply(fake: FakeRepo) -> FakeRepo:
        import bot.services.mail as mail_module

        for name in (
            "find_quote_by_message_id",
            "find_quote_by_thread",
            "find_quote_by_token",
            "find_quote_by_sender",
        ):
            monkeypatch.setattr(mail_module.repo, name, getattr(fake, name))
        return fake

    return _apply


# --- Порядок матчинга ----------------------------------------------------


async def test_message_id_wins_and_stops_the_chain(patch_repo: Any) -> None:
    """Первый шаг сработал — остальные даже не пробуем."""
    fake = patch_repo(
        FakeRepo(
            by_message_id=FakeQuote(1, "правильный"),
            by_thread=FakeQuote(2, "другой"),
            by_token=FakeQuote(3, "третий"),
            by_sender=FakeQuote(4, "четвёртый"),
        )
    )
    headers = ReplyHeaders(
        thread_id="t-1",
        subject="[RFQ-2026-041] Re: Запрос цены",
        from_email="sales@supplier.ru",
        message_ids=["<abc@medisent>"],
    )

    result = await match_quote(None, headers)

    assert result.method == "message_id"
    assert result.quote is not None and result.quote.id == 1
    assert fake.tried == ["message_id"]


async def test_falls_through_to_thread(patch_repo: Any) -> None:
    fake = patch_repo(
        FakeRepo(by_thread=FakeQuote(2, "по треду"), by_sender=FakeQuote(9, "по адресу"))
    )
    headers = ReplyHeaders(
        thread_id="t-1",
        subject="Re: без токена",
        from_email="sales@supplier.ru",
        message_ids=["<неизвестный@чужой>"],
    )

    result = await match_quote(None, headers)

    assert result.method == "thread"
    assert fake.tried == ["message_id", "thread"]


async def test_falls_through_to_token_in_subject(patch_repo: Any) -> None:
    fake = patch_repo(FakeRepo(by_token=FakeQuote(3, "по токену")))
    headers = ReplyHeaders(
        thread_id=None,
        subject="RE: [RFQ-2026-041] Запрос цены — тонометр",
        from_email="secretary@supplier.ru",
    )

    result = await match_quote(None, headers)

    assert result.method == "token"
    # Ни Message-ID, ни threadId в письме нет — эти шаги пропускаются, а не
    # ходят в базу за заведомо пустым ответом.
    assert fake.tried == ["token"]


async def test_sender_is_the_last_resort(patch_repo: Any) -> None:
    """Адрес пробуется только когда не сработало вообще ничего."""
    fake = patch_repo(FakeRepo(by_sender=FakeQuote(4, "по адресу")))
    headers = ReplyHeaders(subject="Коммерческое предложение", from_email="sales@supplier.ru")

    result = await match_quote(None, headers)

    assert result.method == "sender"
    assert fake.tried == ["sender"]


async def test_full_chain_order_when_every_header_is_present(patch_repo: Any) -> None:
    """Все заголовки на месте, совпадение только по адресу — значит все четыре
    шага должны быть испробованы, и ровно в заданном порядке."""
    fake = patch_repo(FakeRepo(by_sender=FakeQuote(4, "по адресу")))
    headers = ReplyHeaders(
        thread_id="t-1",
        subject="[RFQ-2026-041] Запрос цены",
        from_email="sales@supplier.ru",
        message_ids=["<чужой@другой>"],
    )

    result = await match_quote(None, headers)

    assert result.method == "sender"
    assert fake.tried == ["message_id", "thread", "token", "sender"]


async def test_no_match_returns_none(patch_repo: Any) -> None:
    patch_repo(FakeRepo())
    headers = ReplyHeaders(subject="Спам", from_email="stranger@example.com")

    result = await match_quote(None, headers)

    assert result.quote is None
    assert result.method == "none"


# --- Кейс приёмки этапа 7 ------------------------------------------------


async def test_reply_from_another_address_of_same_domain(patch_repo: Any) -> None:
    """Критерий приёмки ТЗ дословно.

    Письмо ушло на sales@supplier.ru, ответил director@supplier.ru. По адресу
    такое не найдётся — и не должно: заявка привязывается по заголовку цепочки.
    """
    fake = patch_repo(FakeRepo(by_message_id=FakeQuote(1, "исходная заявка"), by_sender=None))
    headers = ReplyHeaders(
        thread_id="t-77",
        subject="Re: [RFQ-2026-041] Запрос цены — тонометр UA-777",
        from_email="director@supplier.ru",  # писали не сюда
        message_ids=["<нашmsgid@medisent>"],  # но цепочка сохранилась
    )

    result = await match_quote(None, headers)

    assert result.method == "message_id"
    assert result.quote is not None and result.quote.id == 1
    assert "sender" not in fake.tried


# --- Разбор заголовков ---------------------------------------------------


@pytest.mark.parametrize(
    ("subject", "expected"),
    [
        ("[RFQ-2026-041] Запрос цены", "RFQ-2026-041"),
        ("Re: [RFQ-2026-041] Запрос цены — тонометр", "RFQ-2026-041"),
        ("RE: RE: FWD: [rfq-2026-7] что-то", "RFQ-2026-7"),
        ("Ответ по заявке RFQ-2025-999 от вчера", "RFQ-2025-999"),
        ("Коммерческое предложение", None),
        ("", None),
    ],
    ids=["в скобках", "с Re", "нижний регистр", "без скобок", "нет токена", "пусто"],
)
def test_extract_token(subject: str, expected: str | None) -> None:
    assert extract_token(subject) == expected


def test_parse_message_ids_collects_from_both_headers() -> None:
    ids = parse_message_ids("<a@x>", "<b@y> <c@z>")
    assert ids == ["<a@x>", "<b@y>", "<c@z>"]


def test_parse_message_ids_deduplicates() -> None:
    assert parse_message_ids("<a@x>", "<a@x> <b@y>") == ["<a@x>", "<b@y>"]


def test_parse_message_ids_handles_empty() -> None:
    assert parse_message_ids(None, "") == []


def test_parse_message_ids_ignores_garbage() -> None:
    assert parse_message_ids("не заголовок вовсе") == []


# --- Разбор письма из Gmail ----------------------------------------------


def _b64(text: str) -> str:
    return base64.urlsafe_b64encode(text.encode("utf-8")).decode("ascii").rstrip("=")


def test_parse_message_extracts_everything_needed() -> None:
    payload = {
        "id": "18f2c",
        "threadId": "t-77",
        "snippet": "фрагмент",
        "payload": {
            "mimeType": "multipart/mixed",
            "headers": [
                {"name": "Subject", "value": "Re: [RFQ-2026-041] Запрос цены"},
                {"name": "From", "value": "Отдел продаж <Sales@Supplier.RU>"},
                {"name": "In-Reply-To", "value": "<наш@medisent>"},
                {"name": "References", "value": "<наш@medisent> <ещё@supplier>"},
            ],
            "parts": [
                {
                    "mimeType": "text/plain",
                    "body": {"data": _b64("Цена 12 500 руб. за штуку, срок 5 дней.")},
                },
                {
                    "mimeType": "application/pdf",
                    "filename": "прайс.pdf",
                    "body": {"attachmentId": "att-1", "size": 40960},
                },
            ],
        },
    }

    headers = parse_message(payload)

    assert headers.gmail_id == "18f2c"
    assert headers.thread_id == "t-77"
    assert headers.from_email == "sales@supplier.ru"  # приведён к нижнему регистру
    assert headers.from_name == "Отдел продаж"
    assert headers.message_ids == ["<наш@medisent>", "<ещё@supplier>"]
    assert "12 500" in headers.body
    assert len(headers.attachments) == 1
    assert headers.attachments[0]["filename"] == "прайс.pdf"


def test_parse_message_falls_back_to_snippet_without_text_part() -> None:
    payload = {
        "id": "1",
        "threadId": "t",
        "snippet": "только фрагмент",
        "payload": {"headers": [{"name": "From", "value": "a@b.ru"}]},
    }
    assert parse_message(payload).body == "только фрагмент"


def test_parse_message_survives_nested_multipart() -> None:
    """Вложенный multipart/alternative — обычное дело у корпоративной почты."""
    payload = {
        "id": "1",
        "threadId": "t",
        "payload": {
            "mimeType": "multipart/mixed",
            "headers": [{"name": "From", "value": "a@b.ru"}],
            "parts": [
                {
                    "mimeType": "multipart/alternative",
                    "parts": [
                        {"mimeType": "text/plain", "body": {"data": _b64("вложенный текст")}},
                        {"mimeType": "text/html", "body": {"data": _b64("<p>html</p>")}},
                    ],
                }
            ],
        },
    }
    assert parse_message(payload).body == "вложенный текст"


# --- Сборка исходящего письма --------------------------------------------


def test_built_message_carries_token_in_subject() -> None:
    """Токен в теме — это третий шаг матчинга, без него он не сработает."""
    raw, message_id = build_message(
        sender="buyer@company.ru",
        sender_name="",
        to="sales@supplier.ru",
        token="RFQ-2026-041",
        subject_suffix="Запрос цены — тонометр",
        body="Здравствуйте.",
    )
    decoded = base64.urlsafe_b64decode(raw + "==").decode("utf-8")

    assert "[RFQ-2026-041]" in decoded.replace("=\n", "")
    assert message_id.startswith("<") and message_id.endswith(">")
    assert "company.ru" in message_id


def test_built_message_id_is_unique_per_call() -> None:
    """Свой Message-ID у каждого письма: по нему ищется ответ."""
    _, first = build_message(
        sender="a@b.ru",
        sender_name="",
        to="x@y.ru",
        token="RFQ-2026-1",
        subject_suffix="s",
        body="b",
    )
    _, second = build_message(
        sender="a@b.ru",
        sender_name="",
        to="x@y.ru",
        token="RFQ-2026-1",
        subject_suffix="s",
        body="b",
    )
    assert first != second


def test_extracted_token_round_trips_through_built_subject() -> None:
    """Что положили в тему, то и достаём обратно."""
    token = "RFQ-2026-041"
    raw, _ = build_message(
        sender="a@b.ru",
        sender_name="",
        to="x@y.ru",
        token=token,
        subject_suffix="Запрос цены",
        body="b",
    )
    decoded = base64.urlsafe_b64decode(raw + "==").decode("utf-8").replace("=\n", "")
    subject_line = next(line for line in decoded.splitlines() if line.startswith("Subject:"))
    assert extract_token(subject_line) == token
