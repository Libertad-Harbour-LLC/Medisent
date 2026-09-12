"""Оценочный набор на промпт отчёта.

Модель здесь не вызывается: настоящие вызовы стоят денег и дают разный ответ
каждый прогон, а проверять надо не формулировки, а **инварианты вокруг них**.
Ответы модели записаны в `cases/*.json`, включая заведомо плохие.

Инварианты, которые обязаны держаться при любом ответе модели:

1. кандидат, которого модели не давали, в отчёт не попадает;
2. кандидат, которого модель не упомянула, из отчёта не пропадает;
3. `unavailable` не превращается в `not_found`;
4. поля реестра и сайта не сливаются ни в структуре, ни в тексте;
5. мусор в ответе не роняет сборку отчёта.

Когда владелец заменит заглушки промптов своими текстами, набор пригодится
как регрессия: правка `prompts/report.md` не должна ломать ни один пункт.
"""

from __future__ import annotations

from decimal import Decimal
from typing import Any

import pytest

from bot.db.models import RegistryState
from bot.services import report as report_module
from bot.services.report import CandidateView, Report, rank_candidates, render
from tests.evals.conftest import load_case


def make_candidates() -> list[CandidateView]:
    """Три кандидата: найден в реестре, не найден, проверить не удалось."""
    return [
        CandidateView(
            candidate_id=1,
            supplier_id=10,
            supplier_name="ООО «Медтехника»",
            domain="medtech.ru",
            email="sales@medtech.ru",
            phone="+7 495 000-00-00",
            site_claims=True,
            site_url="https://medtech.ru/t",
            site_price=Decimal("12500"),
            ru_number="ФСР 2010/08183",
            ru_holder="АО «Эй энд Ди РУС»",
            ru_valid=True,
            ru_registry="misearch",
            registry_state=RegistryState.FOUND,
        ),
        CandidateView(
            candidate_id=2,
            supplier_id=20,
            supplier_name="ООО «Второй»",
            domain="second.ru",
            email="a@second.ru",
            phone=None,
            site_claims=False,
            site_url=None,
            site_price=None,
            ru_number=None,
            ru_holder=None,
            ru_valid=None,
            ru_registry=None,
            registry_state=RegistryState.NOT_FOUND,
        ),
        CandidateView(
            candidate_id=3,
            supplier_id=30,
            supplier_name="ООО «Третий»",
            domain="third.ru",
            email=None,
            phone="+7 812 000-00-00",
            site_claims=None,
            site_url=None,
            site_price=None,
            ru_number=None,
            ru_holder=None,
            ru_valid=None,
            ru_registry=None,
            registry_state=RegistryState.UNAVAILABLE,
        ),
    ]


@pytest.fixture
def replay(monkeypatch: pytest.MonkeyPatch) -> Any:
    """Подменяет вызов модели записанным ответом."""

    def _apply(case_name: str) -> None:
        payload = load_case(case_name)["model_output"]

        class FakeGemini:
            async def run_prompt_file(self, *args: Any, **kwargs: Any) -> dict[str, Any]:
                return dict(payload)

        monkeypatch.setattr(report_module, "get_gemini_service", lambda: FakeGemini())

    return _apply


async def rank(case_name: str) -> tuple[list[CandidateView], str, list[str], bool]:
    return await rank_candidates(
        make_candidates(),
        product="Тонометр",
        qty="10 штук",
        requirements=[],
        criteria=[],
        request_id=1,
    )


# --- 1. Выдуманный кандидат не попадает в отчёт --------------------------


async def test_invented_id_is_dropped(replay: Any) -> None:
    replay("invented_id")
    ordered, _, _, failed = await rank("invented_id")

    assert not failed
    assert {c.candidate_id for c in ordered} == {1, 2, 3}
    assert 999 not in {c.candidate_id for c in ordered}


# --- 2. Неупомянутый кандидат не теряется --------------------------------


async def test_unmentioned_candidates_survive(replay: Any) -> None:
    """Модель назвала одного из трёх — остальные уходят в хвост, а не в мусор."""
    replay("dropped_candidates")
    ordered, _, _, _ = await rank("dropped_candidates")

    assert len(ordered) == 3
    assert ordered[0].candidate_id == 2  # тот, кого модель поставила первым
    assert {c.rank for c in ordered} == {1, 2, 3}


# --- 3. Три состояния реестра не путаются --------------------------------


async def test_three_registry_states_stay_distinct(replay: Any) -> None:
    replay("happy_path")
    ordered, summary, missing, _ = await rank("happy_path")
    text = "\n".join(render(Report("RFQ-2026-001", "Тонометр", ordered, summary, missing)))

    assert "ФСР 2010/08183" in text
    assert "в реестрах не найдено" in text
    assert "проверить не удалось" in text


async def test_unavailable_never_reads_as_not_found(replay: Any) -> None:
    """Самая дорогая ошибка модуля: «не смогли проверить» выдать за «нет РУ»."""
    replay("happy_path")
    ordered, summary, missing, _ = await rank("happy_path")
    third = next(c for c in ordered if c.candidate_id == 3)
    text = "\n".join(render(Report("RFQ-2026-001", "Тонометр", [third], summary, missing)))

    assert "проверить не удалось" in text
    assert "в реестрах не найдено" not in text


# --- 4. Поля не сливаются ни в структуре, ни в тексте --------------------


async def test_conflation_in_model_text_is_scrubbed(replay: Any) -> None:
    """Инструкция в промпте — не защита. Правило проверяется кодом."""
    replay("conflation")
    ordered, summary, _, _ = await rank("conflation")

    assert "проверен в Росздравнадзоре" not in ordered[0].reason
    assert "аккредитован" not in summary.lower()
    assert "формулировка убрана" in ordered[0].reason


@pytest.mark.parametrize(
    "text",
    [
        "Поставщик проверен в Росздравнадзоре",
        "поставщик надёжный, проверенный",
        "Компания аккредитована в Росздравнадзоре",
        "поставщик зарегистрирован в реестре",
        "проверено в Росздравнадзоре",
    ],
    ids=["прямо", "мягко", "аккредитован", "зарегистрирован", "безлично"],
)
def test_conflation_phrasings_are_caught(text: str) -> None:
    assert report_module.find_conflation(text) is not None


@pytest.mark.parametrize(
    "text",
    [
        "РУ на изделие действует, держатель АО «Эй энд Ди РУС»",
        "Поставщик заявляет наличие на своём сайте",
        "Изделие зарегистрировано, номер ФСР 2010/08183",
        "Цена ниже средней, срок поставки пять дней",
    ],
    ids=["про изделие", "про сайт", "изделие зарегистрировано", "нейтрально"],
)
def test_legitimate_phrasings_are_left_alone(text: str) -> None:
    """Валидатор не должен резать правильные формулировки."""
    assert report_module.find_conflation(text) is None


async def test_registry_and_site_are_separate_keys_in_the_prompt(replay: Any) -> None:
    """Даже на входе в модель это разные ветки JSON."""
    from bot.services.report import build_payload

    payload = build_payload(
        make_candidates(), product="Тонометр", qty="10", requirements=[], criteria=[]
    )
    candidate = payload["candidates"][0]
    assert "ru_number" in candidate["registry"]
    assert "ru_number" not in candidate
    assert candidate["site_claims"] is True
    assert "site_claims" not in candidate["registry"]


# --- 5. Мусор от модели не роняет отчёт ----------------------------------


async def test_malformed_model_output_does_not_break_the_report(replay: Any) -> None:
    replay("malformed")
    ordered, summary, missing, failed = await rank("malformed")

    assert not failed
    assert len(ordered) == 3, "кандидаты не должны исчезнуть из-за мусора в ответе"
    assert missing == []
    text = "\n".join(render(Report("RFQ-2026-001", "Тонометр", ordered, summary, missing)))
    assert "ООО «Медтехника»" in text


async def test_model_failure_still_produces_a_report(monkeypatch: pytest.MonkeyPatch) -> None:
    """Модель недоступна — собранные данные терять нельзя."""
    from bot.services.gemini import GeminiError

    class BrokenGemini:
        async def run_prompt_file(self, *args: Any, **kwargs: Any) -> dict[str, Any]:
            raise GeminiError("модель недоступна")

    monkeypatch.setattr(report_module, "get_gemini_service", lambda: BrokenGemini())
    ordered, summary, missing, failed = await rank("happy_path")

    assert failed is True
    assert len(ordered) == 3
    text = "\n".join(
        render(Report("RFQ-2026-001", "Т", ordered, summary, missing, llm_failed=True))
    )
    assert "Ранжирование не сработало" in text


# --- Инъекция в названии поставщика --------------------------------------


async def test_injection_in_supplier_name_is_visible_to_the_owner(replay: Any) -> None:
    """Название придумала модель поиска по чужой странице.

    Помеченного кандидата владелец должен увидеть с предупреждением, а не
    получить молча отранжированным.
    """
    replay("happy_path")
    candidates = make_candidates()
    candidates[0].injection_suspected = True
    text = "\n".join(render(Report("RFQ-2026-001", "Тонометр", candidates)))

    assert "попытку повлиять на отбор" in text


async def test_report_payload_goes_to_the_model_wrapped(monkeypatch: pytest.MonkeyPatch) -> None:
    """Кандидаты уходят в модель в явной рамке «это данные, не инструкции»."""
    seen: dict[str, Any] = {}

    class SpyGemini:
        async def run_prompt_file(self, name: str, payload: Any, **kwargs: Any) -> dict[str, Any]:
            seen.update(kwargs)
            return {"ranked": [], "summary": "", "missing_data": []}

    monkeypatch.setattr(report_module, "get_gemini_service", lambda: SpyGemini())
    await rank("happy_path")

    assert seen.get("untrusted") is True
