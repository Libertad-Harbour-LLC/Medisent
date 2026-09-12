"""Тесты потолка расходов и безопасности отправки письма.

Два свойства, за которыми здесь следят:

* заявка не может потратить больше своего потолка — деньги не должны уходить,
  а не «мы узнаём об этом первыми»;
* отправка письма не повторяется вслепую — иначе поставщик получит дубликат.
"""

from __future__ import annotations

from decimal import Decimal
from typing import Any

import pytest

from bot.services import budget
from bot.services.http import CallResult
from bot.services.mail import MailService


@pytest.fixture(autouse=True)
def _clean_budget() -> Any:
    budget.reset()
    yield
    budget.reset()


@pytest.fixture
def gmail_configured(monkeypatch: pytest.MonkeyPatch) -> None:
    """Настройки Gmail для тестов: сервис отказывается работать без них."""
    from bot.config import get_settings

    settings = get_settings()
    for name, value in (
        ("google_client_id", "test-id"),
        ("google_client_secret", "test-secret"),
        ("google_refresh_token", "1//test-refresh"),
        ("gmail_sender", "bot@example.com"),
    ):
        monkeypatch.setattr(settings, name, value)


@pytest.fixture
def no_db_seed(monkeypatch: pytest.MonkeyPatch) -> None:
    """Счётчик подсевается из базы; в этих тестах базы нет."""

    async def _zero(request_id: int) -> Decimal:
        return Decimal(0)

    monkeypatch.setattr(budget, "_seed", _zero)


# --- Потолок на заявку ----------------------------------------------------


async def test_spending_under_the_cap_is_allowed(no_db_seed: None) -> None:
    assert await budget.allow(1, Decimal("0.01")) is True


async def test_spending_over_the_cap_is_refused(
    no_db_seed: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Потолок по умолчанию $0.50 — один вызов дороже него не уходит."""
    assert await budget.allow(1, Decimal("10.00")) is False
    assert budget.exceeded(1) is True


async def test_cap_accumulates_across_calls(no_db_seed: None) -> None:
    """Десять скрейпов по чуть-чуть в сумме тоже упираются в потолок."""
    for _ in range(50):
        if await budget.allow(1, Decimal("0.02")):
            budget.record(1, Decimal("0.02"))
    assert budget.spent(1) <= Decimal("0.50")
    assert budget.exceeded(1) is True


async def test_free_calls_are_never_limited(no_db_seed: None) -> None:
    """Реестры и Gmail бесплатны: потолок про деньги, а не про число запросов."""
    budget.record(1, Decimal("0.50"))
    assert await budget.allow(1, Decimal(0)) is True
    assert await budget.allow(1, None) is True


async def test_calls_outside_a_request_are_not_limited(no_db_seed: None) -> None:
    assert await budget.allow(None, Decimal("100")) is True


async def test_requests_have_separate_caps(no_db_seed: None) -> None:
    budget.record(1, Decimal("0.49"))
    assert await budget.allow(1, Decimal("0.05")) is False
    assert await budget.allow(2, Decimal("0.05")) is True


async def test_forget_releases_the_counter(no_db_seed: None) -> None:
    budget.record(1, Decimal("0.60"))
    assert await budget.allow(1, Decimal("0.01")) is False
    budget.forget(1)
    assert await budget.allow(1, Decimal("0.01")) is True


async def test_seed_restores_spending_after_restart(monkeypatch: pytest.MonkeyPatch) -> None:
    """После перезапуска потолок восстанавливается из базы, а не обнуляется."""

    async def _already_spent(request_id: int) -> Decimal:
        return Decimal("0.49")

    monkeypatch.setattr(budget, "_seed", _already_spent)
    assert await budget.allow(7, Decimal("0.05")) is False


# --- Отправка письма ------------------------------------------------------


class RecordingClient:
    """Запоминает, с какими параметрами звали, и отдаёт заготовленный ответ."""

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


def make_service(results: list[CallResult]) -> tuple[MailService, RecordingClient]:
    service = MailService()
    client = RecordingClient(results)
    service._client = client  # type: ignore[assignment]
    service._access_token = "test-token"
    import datetime as dt

    service._expires_at = dt.datetime.now(dt.UTC) + dt.timedelta(hours=1)
    return service, client


async def test_send_disables_retries(gmail_configured: None) -> None:
    """Слепой повтор отправки = второе письмо поставщику."""
    service, client = make_service([CallResult(ok=True, status_code=200, json={"threadId": "t"})])

    await service.send(
        to="sales@supplier.ru",
        token="RFQ-2026-001",
        subject_suffix="Запрос цены",
        body="Здравствуйте.",
    )

    send_call = next(c for c in client.calls if c["url"].endswith("/messages/send"))
    assert send_call["retries"] == 0


async def test_failed_send_checks_whether_the_letter_is_already_there(
    gmail_configured: None,
) -> None:
    """Gmail мог принять письмо и не успеть ответить.

    Тогда «не отправлено» — неправда, и повторять нельзя. Проверяем по
    собственному Message-ID.
    """
    service, client = make_service(
        [
            CallResult(ok=False, status_code=504, error="таймаут"),
            CallResult(ok=True, status_code=200, json={"messages": [{"id": "m1"}]}),
            CallResult(ok=True, status_code=200, json={"threadId": "t-found"}),
        ]
    )

    thread_id, message_id = await service.send(
        to="sales@supplier.ru",
        token="RFQ-2026-001",
        subject_suffix="Запрос цены",
        body="текст",
    )

    assert thread_id == "t-found"
    assert message_id.startswith("<")
    # Отправляли ровно один раз, дальше только читали.
    assert sum(1 for c in client.calls if c["url"].endswith("/messages/send")) == 1


async def test_failed_send_without_a_copy_raises(gmail_configured: None) -> None:
    """Письма в ящике нет — значит действительно не ушло, надо сказать честно."""
    from bot.services.mail import MailError

    service, _ = make_service(
        [
            CallResult(ok=False, status_code=500, error="сервер лёг"),
            CallResult(ok=True, status_code=200, json={"messages": []}),
        ]
    )

    with pytest.raises(MailError):
        await service.send(to="a@b.ru", token="RFQ-2026-001", subject_suffix="s", body="b")


async def test_forward_also_disables_retries(gmail_configured: None) -> None:
    service, client = make_service([CallResult(ok=True, status_code=200, json={})])
    await service.forward_file(
        to="me@example.com",
        filename="прайс.pdf",
        content=b"%PDF-1.4",
        mime_type="application/pdf",
    )
    assert client.calls[0]["retries"] == 0


async def test_token_refresh_redacts_the_response_body(gmail_configured: None) -> None:
    """В ответе Google приходят access_token и refresh_token.

    Одна отладочная строка с CallResult — и токен в файле лога, а по нему
    читается вся почта владельца.
    """
    service = MailService()
    client = RecordingClient(
        [CallResult(ok=True, status_code=200, json={"access_token": "ya29.x", "expires_in": 3600})]
    )
    service._client = client  # type: ignore[assignment]

    await service._token()

    assert client.calls[0]["redact_body"] is True
