"""Тесты проверки в реестрах Росздравнадзора.

ТЗ требует тесты именно на этот модуль: ошибка здесь тихая и дорогая.
Три случая приёмки — изделие до 01.03.2025, после, и несуществующее — плюс
отдельно самое опасное: «не смогли проверить» не должно превращаться в
«не нашли».
"""

from __future__ import annotations

from typing import Any

import pytest

from bot.db.models import RegistryState
from bot.services import registry_endpoints as endpoints
from bot.services.http import CallResult
from bot.services.registry import RegistryService, cache_key
from tests.conftest import load_fixture, load_json_fixture


class FakeClient:
    """Подменяет ApiClient: отдаёт заранее записанные ответы реестров."""

    def __init__(self, *, elk: CallResult, misearch: CallResult) -> None:
        self.elk = elk
        self.misearch = misearch
        self.calls: list[str] = []

    async def post(self, url: str, **kwargs: Any) -> CallResult:
        self.calls.append(f"POST {url}")
        return self.elk

    async def get(self, url: str, **kwargs: Any) -> CallResult:
        self.calls.append(f"GET {url}")
        return self.misearch

    async def aclose(self) -> None:
        return None


def make_service(*, elk: CallResult, misearch: CallResult) -> RegistryService:
    service = RegistryService()
    service._client = FakeClient(elk=elk, misearch=misearch)  # type: ignore[assignment]
    return service


def ok_json(payload: Any) -> CallResult:
    return CallResult(ok=True, status_code=200, json=payload)


def ok_html(text: str) -> CallResult:
    return CallResult(ok=True, status_code=200, text=text)


def failed(error: str) -> CallResult:
    return CallResult(ok=False, status_code=503, error=error)


# --- Три случая приёмки --------------------------------------------------


async def test_registered_after_march_2025_comes_from_elk() -> None:
    """Изделие, зарегистрированное после 01.03.2025, находится в elk."""
    service = make_service(
        elk=ok_json(load_json_fixture("elk_found.json")),
        misearch=ok_html(load_fixture("misearch_not_found.html")),
    )

    result = await service.check_product("Аппарат ИВЛ портативный")

    assert result.state == RegistryState.FOUND
    assert result.best is not None
    assert result.best.registry == "elk"
    assert result.best.ru_number == "РЗН 2025/24118"
    assert result.best.holder == "ООО «МедТехника Плюс»"
    assert result.best.valid is True
    assert result.errors == {}


async def test_registered_before_march_2025_comes_from_misearch() -> None:
    """Изделие до 01.03.2025 находится в старом реестре."""
    service = make_service(
        elk=ok_json(load_json_fixture("elk_empty.json")),
        misearch=ok_html(load_fixture("misearch_found.html")),
    )

    result = await service.check_product("Тонометр UA-777", ru_number="ФСР 2010/08183")

    assert result.state == RegistryState.FOUND
    assert result.best is not None
    assert result.best.registry == "misearch"
    assert result.best.ru_number == "ФСР 2010/08183"
    assert "Эй энд Ди" in (result.best.holder or "")
    assert result.best.valid is True


async def test_nonexistent_product_is_not_found() -> None:
    """Оба реестра ответили внятно и оба сказали «нет» → not_found."""
    service = make_service(
        elk=ok_json(load_json_fixture("elk_empty.json")),
        misearch=ok_html(load_fixture("misearch_not_found.html")),
    )

    result = await service.check_product("Изделие которого не существует 12345")

    assert result.state == RegistryState.NOT_FOUND
    assert result.records == []
    assert result.errors == {}


# --- Главное правило модуля ----------------------------------------------


async def test_both_registries_down_is_unavailable_not_not_found() -> None:
    """Реестры недоступны → unavailable. Это не «не нашли»."""
    service = make_service(elk=failed("таймаут"), misearch=failed("HTTP 503"))

    result = await service.check_product("Тонометр UA-777")

    assert result.state == RegistryState.UNAVAILABLE
    assert result.state != RegistryState.NOT_FOUND
    assert set(result.errors) == {"elk", "misearch"}


async def test_one_registry_down_and_nothing_found_is_unavailable() -> None:
    """Один реестр молчит, второй ничего не нашёл — проверка неполная.

    Сказать «не найдено» здесь нельзя: изделие может быть как раз в том
    реестре, который не ответил.
    """
    service = make_service(
        elk=failed("connection reset"),
        misearch=ok_html(load_fixture("misearch_not_found.html")),
    )

    result = await service.check_product("Тонометр UA-777")

    assert result.state == RegistryState.UNAVAILABLE
    assert "elk" in result.errors


async def test_one_registry_down_but_other_found_is_found() -> None:
    """Нашли в живом реестре — результат есть, ошибка второго только логируется."""
    service = make_service(
        elk=failed("таймаут"),
        misearch=ok_html(load_fixture("misearch_found.html")),
    )

    result = await service.check_product("Тонометр UA-777")

    assert result.state == RegistryState.FOUND
    assert result.errors["elk"]


async def test_changed_markup_is_unavailable_not_not_found() -> None:
    """Страница отдалась, но разметка не та — самый коварный случай.

    Пустая выдача из-за смены вёрстки не должна выглядеть как «изделие не
    зарегистрировано»: по такому отчёту закупщик откажется от нормального
    поставщика.
    """
    service = make_service(
        elk=ok_json(load_json_fixture("elk_unknown_shape.json")),
        misearch=ok_html(load_fixture("misearch_changed_markup.html")),
    )

    result = await service.check_product("Тонометр UA-777")

    assert result.state == RegistryState.UNAVAILABLE
    assert "не разобран" in result.errors["elk"]
    assert "не разобрана" in result.errors["misearch"]


# --- Разбор ответов ------------------------------------------------------


def test_elk_empty_list_is_understood_as_nothing_found() -> None:
    outcome = endpoints.parse_elk_payload({"content": [], "totalElements": 0})
    assert outcome.understood is True
    assert outcome.records == []


def test_elk_unknown_shape_is_not_understood() -> None:
    outcome = endpoints.parse_elk_payload({"whatever": 1})
    assert outcome.understood is False


def test_elk_nonempty_list_with_mismatching_total_is_not_understood() -> None:
    """Пустой список при total>0 — признак, что мы читаем не то поле."""
    outcome = endpoints.parse_elk_payload({"content": [], "totalElements": 17})
    assert outcome.understood is False


def test_misearch_not_found_marker_is_understood() -> None:
    outcome = endpoints.parse_misearch_html(load_fixture("misearch_not_found.html"))
    assert outcome.understood is True
    assert outcome.records == []


def test_misearch_blank_page_without_marker_is_not_understood() -> None:
    outcome = endpoints.parse_misearch_html("<html><body></body></html>")
    assert outcome.understood is False


@pytest.mark.parametrize(
    ("status", "expected"),
    [
        ("Действует", True),
        ("действующее", True),
        ("Отменено", False),
        ("Аннулировано", False),
        ("Приостановлено", False),
        ("Регистрация прекращена", False),
        ("", None),
        (None, None),
        ("Неведомый статус", None),
    ],
    ids=[
        "действует",
        "действующее",
        "отменено",
        "аннулировано",
        "приостановлено",
        "прекращена",
        "пусто",
        "none",
        "неизвестно",
    ],
)
def test_status_parsing(status: str | None, expected: bool | None) -> None:
    assert endpoints._valid_from_status(status) is expected


@pytest.mark.parametrize(
    "raw",
    ["ФСР 2010/08183", "ФСЗ 2011/09876", "РЗН 2025/24118", "фср 2010/08183"],
)
def test_ru_number_regex_matches_all_registry_prefixes(raw: str) -> None:
    assert endpoints.RU_NUMBER_RE.search(raw) is not None


# --- Кэш -----------------------------------------------------------------


def test_cache_key_ignores_case_and_extra_spaces() -> None:
    assert cache_key("Тонометр  UA-777 ", None) == cache_key("тонометр ua-777", None)


def test_cache_key_separates_different_ru_numbers() -> None:
    assert cache_key("Тонометр", "ФСР 2010/1") != cache_key("Тонометр", "ФСР 2010/2")


async def test_unavailable_is_never_cached() -> None:
    """Закэшировать «не смогли проверить» на 30 дней — значит месяц не проверять."""
    written: list[tuple[str, str]] = []

    class FakeSession:
        pass

    import bot.db.repo as repo_module

    async def fake_get(*args: Any, **kwargs: Any) -> None:
        return None

    async def fake_put(session: Any, key: str, state: str, payload: Any) -> None:
        written.append((key, state))

    original_get, original_put = repo_module.get_registry_cache, repo_module.put_registry_cache
    repo_module.get_registry_cache = fake_get  # type: ignore[assignment]
    repo_module.put_registry_cache = fake_put  # type: ignore[assignment]
    try:
        service = make_service(elk=failed("таймаут"), misearch=failed("таймаут"))
        result = await service.check_product("Тонометр", session=FakeSession())
        assert result.state == RegistryState.UNAVAILABLE
        assert written == []

        service_ok = make_service(
            elk=ok_json(load_json_fixture("elk_found.json")),
            misearch=ok_html(load_fixture("misearch_not_found.html")),
        )
        await service_ok.check_product("Аппарат ИВЛ", session=FakeSession())
        assert [state for _, state in written] == [RegistryState.FOUND]
    finally:
        repo_module.get_registry_cache = original_get  # type: ignore[assignment]
        repo_module.put_registry_cache = original_put  # type: ignore[assignment]


# --- Запрет на LLM в этом модуле -----------------------------------------


def test_registry_module_does_not_import_any_llm() -> None:
    """Проверка реестра — детерминированный код. Подмена ответом модели
    запрещена при любых обстоятельствах, поэтому импортов LLM тут быть не может."""
    from pathlib import Path

    source = Path("bot/services/registry.py").read_text(encoding="utf-8")
    forbidden = ("gemini", "openai", "anthropic", "llm", "generateContent")
    for name in forbidden:
        assert name not in source.lower(), f"в registry.py просочился {name}"
