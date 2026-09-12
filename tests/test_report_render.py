"""Тесты отчёта.

Главное здесь — критерий приёмки этапа 5: «ни одно поле не смешивает данные
реестра и данные сайта». Это не стилистика, а суть: «изделие зарегистрировано
в Росздравнадзоре» и «поставщик пишет у себя, что оно есть» — разные факты, и
из вторго не следует первое.
"""

from __future__ import annotations

from decimal import Decimal

import pytest

from bot.db.models import RegistryState
from bot.services.report import (
    TELEGRAM_LIMIT,
    CandidateView,
    Report,
    build_payload,
    render,
)


def make_view(**kwargs: object) -> CandidateView:
    defaults: dict[str, object] = {
        "candidate_id": 1,
        "supplier_id": 10,
        "supplier_name": "ООО «Медтехника»",
        "domain": "medtech.ru",
        "email": "sales@medtech.ru",
        "phone": "+7 495 000-00-00",
        "site_claims": True,
        "site_url": "https://medtech.ru/tonometr",
        "site_price": Decimal("12500"),
        "ru_number": "ФСР 2010/08183",
        "ru_holder": "АО «Эй энд Ди РУС»",
        "ru_valid": True,
        "ru_registry": "misearch",
        "registry_state": RegistryState.FOUND,
    }
    defaults.update(kwargs)
    return CandidateView(**defaults)  # type: ignore[arg-type]


def render_one(view: CandidateView) -> str:
    return "\n".join(render(Report(request_token="RFQ-2026-041", product="Тонометр", candidates=[view])))


# --- Критерий приёмки этапа 5 -------------------------------------------


def test_registry_and_site_are_separate_labelled_lines() -> None:
    text = render_one(make_view())

    assert "РУ на изделие:" in text
    assert "Поставщик:" in text
    # Строки разные и обе на месте.
    ru_line = next(line for line in text.splitlines() if "РУ на изделие:" in line)
    site_line = next(line for line in text.splitlines() if "Поставщик:" in line)
    assert "ФСР 2010/08183" in ru_line
    assert "ФСР" not in site_line
    assert "заявляет наличие" in site_line
    assert "заявляет наличие" not in ru_line


def test_report_never_says_supplier_is_verified_by_roszdravnadzor() -> None:
    """Формулировка, которую ТЗ запрещает прямым текстом."""
    text = render_one(make_view()).lower()
    for forbidden in (
        "поставщик проверен",
        "проверен в росздравнадзоре",
        "поставщик зарегистрирован",
    ):
        assert forbidden not in text


def test_site_claims_without_registry_does_not_imply_registration() -> None:
    """Сайт заявляет наличие, а в реестре ничего нет — и это должно быть видно."""
    text = render_one(
        make_view(site_claims=True, registry_state=RegistryState.NOT_FOUND, ru_number=None)
    )
    assert "в реестрах не найдено" in text
    assert "заявляет наличие" in text


@pytest.mark.parametrize(
    ("state", "ru_number", "expected"),
    [
        (RegistryState.FOUND, "ФСР 2010/08183", "ФСР 2010/08183"),
        (RegistryState.NOT_FOUND, None, "в реестрах не найдено"),
        (RegistryState.UNAVAILABLE, None, "проверить не удалось"),
    ],
    ids=["найдено", "не найдено", "недоступно"],
)
def test_three_registry_states_render_differently(
    state: str, ru_number: str | None, expected: str
) -> None:
    """«Не нашли» и «не смогли проверить» — разные строки в отчёте."""
    text = render_one(make_view(registry_state=state, ru_number=ru_number))
    assert expected in text


def test_unavailable_is_not_rendered_as_not_found() -> None:
    text = render_one(make_view(registry_state=RegistryState.UNAVAILABLE, ru_number=None))
    assert "в реестрах не найдено" not in text
    assert "проверить не удалось" in text


@pytest.mark.parametrize(
    ("claims", "expected"),
    [(True, "заявляет наличие"), (False, "наличие не заявлено"), (None, "сайт не проверен")],
    ids=["да", "нет", "неизвестно"],
)
def test_three_site_states_render_differently(claims: bool | None, expected: str) -> None:
    assert expected in render_one(make_view(site_claims=claims))


# --- Рендер --------------------------------------------------------------


def test_contacts_are_shown_because_stage_six_needs_them() -> None:
    text = render_one(make_view())
    assert "sales@medtech.ru" in text
    assert "+7 495 000-00-00" in text


def test_missing_contacts_are_stated_not_hidden() -> None:
    text = render_one(make_view(email=None, phone=None))
    assert "Контакты: не найдены" in text


def test_long_report_is_split_within_telegram_limit() -> None:
    candidates = [
        make_view(candidate_id=i, supplier_name=f"Поставщик номер {i} " + "и" * 200)
        for i in range(1, 30)
    ]
    chunks = render(Report(request_token="RFQ-2026-1", product="Тонометр", candidates=candidates))
    assert len(chunks) > 1
    assert all(len(chunk) <= TELEGRAM_LIMIT for chunk in chunks)


def test_llm_failure_is_visible_not_silent() -> None:
    report = Report(
        request_token="RFQ-2026-1", product="Т", candidates=[make_view()], llm_failed=True
    )
    assert "Ранжирование не сработало" in "\n".join(render(report))


def test_footer_asks_for_voice_reply() -> None:
    assert "голосов" in render_one(make_view()).lower()


# --- Данные для модели ---------------------------------------------------


def test_payload_keeps_registry_and_site_in_separate_keys() -> None:
    """Даже на входе в модель это разные ветки JSON, а не одно поле."""
    payload = build_payload(
        [make_view()], product="Тонометр", qty="10 штук", requirements=[], criteria=[]
    )
    candidate = payload["candidates"][0]

    assert candidate["registry"]["ru_number"] == "ФСР 2010/08183"
    assert candidate["site_claims"] is True
    assert "ru_number" not in candidate
    assert "site_claims" not in candidate["registry"]


def test_payload_carries_accumulated_criteria() -> None:
    criteria = [{"text": "срок важнее цены", "direction": "plus", "weight": 1.5, "times_seen": 3}]
    payload = build_payload(
        [make_view()], product="Т", qty="1", requirements=["сертификат"], criteria=criteria
    )
    assert payload["criteria"] == criteria
    assert payload["requirements"] == ["сертификат"]
