"""Отчёт по кандидатам.

Схема работы жёсткая: модель получает готовый JSON и возвращает JSON.
**Рендер сообщения делает код, а не модель.** Свободный текст от модели не
принимается — номера РУ и цены поплывут не сразу, а на десятом прогоне.

Второе жёсткое правило: «РУ на изделие» и «поставщик заявляет наличие» —
два независимых поля. Первое из реестра и относится к изделию, второе с сайта
и относится к поставщику. Формулировка «проверено в Росздравнадзоре» про
поставщика в этом файле невозможна: она нигде не собирается.
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass, field
from decimal import Decimal
from typing import Any

from bot import texts
from bot.db.models import RegistryState
from bot.logging_setup import log_extra
from bot.services.gemini import GeminiError, get_gemini_service

logger = logging.getLogger(__name__)

TELEGRAM_LIMIT = 4096

REPORT_SCHEMA: dict[str, Any] = {
    "type": "OBJECT",
    "properties": {
        "ranked": {
            "type": "ARRAY",
            "items": {
                "type": "OBJECT",
                "properties": {
                    "id": {"type": "INTEGER"},
                    "rank": {"type": "INTEGER"},
                    "reason": {"type": "STRING"},
                    "concerns": {"type": "ARRAY", "items": {"type": "STRING"}},
                },
                "required": ["id", "rank", "reason"],
            },
        },
        "summary": {"type": "STRING"},
        "missing_data": {"type": "ARRAY", "items": {"type": "STRING"}},
    },
    "required": ["ranked", "summary"],
}


@dataclass(slots=True)
class CandidateView:
    """Кандидат в том виде, в каком его показывают владельцу."""

    candidate_id: int
    supplier_id: int
    supplier_name: str
    domain: str | None
    email: str | None
    phone: str | None
    site_claims: bool | None
    site_url: str | None
    site_price: Decimal | None
    ru_number: str | None
    ru_holder: str | None
    ru_valid: bool | None
    ru_registry: str | None
    registry_state: str
    unrega_flags: list[str] = field(default_factory=list)
    # На сайте нашёлся текст, похожий на попытку повлиять на отбор.
    injection_suspected: bool = False
    rank: int = 0
    reason: str = ""
    concerns: list[str] = field(default_factory=list)


# Формулировки, которых в тексте модели быть не должно. «РУ на изделие» —
# из реестра и про изделие; «поставщик заявляет наличие» — с сайта и про
# поставщика. Слить их в «поставщик проверен в Росздравнадзоре» запрещено
# правилом проекта, и одной инструкции в промпте для этого мало: правило,
# которое повторяют модели, надо проверять кодом.
FORBIDDEN_CONFLATION = (
    # Порядок важен: длинные варианты идут первыми, иначе после вырезания
    # короткого в тексте останется висящее «в Росздравнадзоре».
    re.compile(
        r"поставщик\w*\s+(?:\S+\s+){0,2}провер\w+(?:\s+в\s+росздравнадзор\w*)?",
        re.IGNORECASE,
    ),
    re.compile(r"аккредитован\w*\s+(?:в\s+)?росздравнадзор\w*", re.IGNORECASE),
    re.compile(r"провер\w+\s+(?:в\s+)?росздравнадзор\w*", re.IGNORECASE),
    re.compile(r"поставщик\w*\s+(?:\S+\s+){0,2}зарегистрирован\w*", re.IGNORECASE),
)


def find_conflation(text: str) -> str | None:
    """Нашлась ли в тексте модели запрещённая формулировка. Возвращает её."""
    for pattern in FORBIDDEN_CONFLATION:
        match = pattern.search(text or "")
        if match:
            return match.group(0)
    return None


def scrub_conflation(text: str, *, where: str, request_id: int | None = None) -> str:
    """Вырезать запрещённую формулировку из текста модели.

    Текст не переписывается: фраза заменяется пометкой, и владелец видит, что
    модель попыталась сказать то, чего говорить нельзя.
    """
    found = find_conflation(text)
    if not found:
        return text
    logger.warning(
        "Модель попыталась слить поля реестра и сайта в «%s» (%s)",
        found,
        where,
        extra=log_extra(request_id),
    )
    cleaned = text
    for pattern in FORBIDDEN_CONFLATION:
        cleaned = pattern.sub(
            "[формулировка убрана: реестр проверяет изделие, не поставщика]", cleaned
        )
    return cleaned


@dataclass(slots=True)
class Report:
    request_token: str
    product: str
    candidates: list[CandidateView]
    summary: str = ""
    missing_data: list[str] = field(default_factory=list)
    llm_failed: bool = False
    # Проверка информационных писем — одна на заявку. Пустая строка — «не
    # применимо» (отчёт собран без конвейера); ``unavailable`` печатается
    # отдельной строкой, чтобы молчание не читалось как «писем нет».
    unrega_state: str = ""


def _registry_line(view: CandidateView) -> str:
    """Строка про РУ. Три исхода, и ни один не подменяет другой."""
    if view.registry_state == RegistryState.UNAVAILABLE:
        return texts.RU_UNAVAILABLE
    if view.registry_state == RegistryState.NOT_FOUND or not view.ru_number:
        return texts.RU_NOT_FOUND
    status = (
        "действует"
        if view.ru_valid
        else ("не действует" if view.ru_valid is False else "статус неизвестен")
    )
    return texts.RU_FOUND.format(
        number=view.ru_number,
        holder=view.ru_holder or "держатель не указан",
        status=status,
        registry=view.ru_registry or "?",
    )


def _site_line(view: CandidateView) -> str:
    """Строка про сайт. Отдельная от строки про реестр — это принципиально."""
    if view.site_claims is True:
        return texts.SITE_CLAIMS_YES
    if view.site_claims is False:
        return texts.SITE_CLAIMS_NO
    return texts.SITE_CLAIMS_UNKNOWN


def build_payload(
    candidates: list[CandidateView],
    *,
    product: str,
    qty: str,
    requirements: list[str],
    criteria: list[dict[str, Any]],
    unrega_state: str = "",
) -> dict[str, Any]:
    """JSON для модели. Больше она ничего не получает."""
    return {
        "product": product,
        "qty": qty,
        "requirements": requirements,
        "criteria": criteria,
        # Состояние проверки писем отдельно от списка: пустой список при
        # ``unavailable`` — это «не проверяли», а не «писем нет».
        "unrega_state": unrega_state,
        "candidates": [
            {
                "id": c.candidate_id,
                "supplier": c.supplier_name,
                "domain": c.domain,
                "email": c.email,
                "phone": c.phone,
                "site_claims": c.site_claims,
                "site_url": c.site_url,
                "site_price": float(c.site_price) if c.site_price is not None else None,
                "registry": {
                    "state": c.registry_state,
                    "ru_number": c.ru_number,
                    "ru_holder": c.ru_holder,
                    "ru_valid": c.ru_valid,
                    "ru_registry": c.ru_registry,
                },
                "unrega_flags": c.unrega_flags,
            }
            for c in candidates
        ],
    }


async def rank_candidates(
    candidates: list[CandidateView],
    *,
    product: str,
    qty: str,
    requirements: list[str],
    criteria: list[dict[str, Any]],
    request_id: int | None = None,
    unrega_state: str = "",
) -> tuple[list[CandidateView], str, list[str], bool]:
    """Ранжирование моделью. Возвращает ``(кандидаты, вывод, чего не хватило, сбой)``.

    Если модель недоступна или ответила не по схеме, отчёт всё равно уходит:
    порядок остаётся исходным, а владелец видит пометку. Молча подсовывать
    выдуманный рейтинг нельзя, но и терять собранные данные из-за сбоя LLM —
    тоже.
    """
    if not candidates:
        return [], "", [], False

    payload = build_payload(
        candidates,
        product=product,
        qty=qty,
        requirements=requirements,
        criteria=criteria,
        unrega_state=unrega_state,
    )
    try:
        parsed = await get_gemini_service().run_prompt_file(
            "report",
            payload,
            schema=REPORT_SCHEMA,
            request_id=request_id,
            operation="report.rank",
            # Названия компаний придумала модель поиска по чужим страницам —
            # это данные, а не инструкции.
            untrusted=True,
        )
    except GeminiError as exc:
        logger.error("Ранжирование не удалось: %s", exc, extra=log_extra(request_id))
        for index, view in enumerate(candidates, start=1):
            view.rank = index
        return candidates, "", [], True

    by_id = {c.candidate_id: c for c in candidates}
    ranked_rows = parsed.get("ranked") or []
    seen: set[int] = set()

    for row in ranked_rows:
        if not isinstance(row, dict):
            continue
        candidate_id = row.get("id")
        matched = by_id.get(_as_id(candidate_id)) if isinstance(candidate_id, int | str) else None
        if matched is None:
            # Модель назвала id, которого мы не давали. Игнорируем: выдуманным
            # кандидатам в отчёте не место.
            logger.warning("Модель вернула неизвестный id=%r", candidate_id)
            continue
        matched.rank = int(row.get("rank") or 0)
        matched.reason = scrub_conflation(
            str(row.get("reason") or "").strip(), where="reason", request_id=request_id
        )
        concerns = row.get("concerns") or []
        # Оговорки — такой же свободный текст модели, как и reason, и уходят
        # владельцу как есть; запрещённая формулировка вырезается и здесь.
        matched.concerns = (
            [
                scrub_conflation(str(c).strip(), where="concerns", request_id=request_id)
                for c in concerns
                if str(c).strip()
            ]
            if isinstance(concerns, list)
            else []
        )
        seen.add(matched.candidate_id)

    # Кандидаты, которых модель не упомянула, не выбрасываются — уходят в хвост.
    tail_rank = max((c.rank for c in candidates if c.rank), default=0)
    for view in candidates:
        if view.candidate_id not in seen:
            tail_rank += 1
            view.rank = tail_rank

    ordered = sorted(candidates, key=lambda c: (c.rank or 10_000, c.candidate_id))
    missing = parsed.get("missing_data") or []
    return (
        ordered,
        scrub_conflation(
            str(parsed.get("summary") or "").strip(), where="summary", request_id=request_id
        ),
        [str(m) for m in missing if str(m).strip()] if isinstance(missing, list) else [],
        False,
    )


def _as_id(value: int | str) -> int:
    """id от модели → int. ``"cand_1"`` — не id, а выдумка: возвращаем то,
    чего в ``by_id`` нет, и кандидат отбрасывается как неизвестный."""
    try:
        return int(value)
    except (TypeError, ValueError):
        return -1


def render(report: Report) -> list[str]:
    """JSON → сообщения Telegram. Разбивка по лимиту в 4096 символов.

    Рендерит код. Модель к тексту, который увидит владелец, не прикасается.
    """
    header = texts.REPORT_HEADER.format(token=report.request_token, product=report.product)
    blocks: list[str] = [header]

    if report.llm_failed:
        blocks.append("⚠️ Ранжирование не сработало, порядок исходный.\n")
    if report.unrega_state == RegistryState.UNAVAILABLE:
        blocks.append(texts.UNREGA_UNAVAILABLE + "\n")

    for index, view in enumerate(report.candidates, start=1):
        lines = [f"<b>{index}. {view.supplier_name}</b>"]
        if view.domain:
            lines.append(f"   {view.domain}")

        # Два независимых поля. Именно так, разными строками, с разными
        # подписями. Сливать их в одно «проверено» запрещено.
        lines.append(f"   РУ на изделие: {_registry_line(view)}")
        lines.append(f"   Поставщик: {_site_line(view)}")

        if view.site_price is not None:
            lines.append(f"   Цена на сайте: {view.site_price:,.0f} ₽".replace(",", " "))
        contacts = " · ".join(filter(None, [view.email, view.phone]))
        lines.append(f"   Контакты: {contacts}" if contacts else "   Контакты: не найдены")

        if view.unrega_flags:
            lines.append(f"   ⚠️ Информационные письма: {len(view.unrega_flags)}")
        if view.injection_suspected:
            lines.append(f"   {texts.INJECTION_SUSPECTED}")
        if view.reason:
            lines.append(f"   <i>{view.reason}</i>")
        for concern in view.concerns:
            lines.append(f"   — {concern}")

        blocks.append("\n".join(lines) + "\n")

    if report.summary:
        blocks.append(f"<b>Вывод:</b> {report.summary}\n")
    if report.missing_data:
        blocks.append("<b>Не хватило данных:</b> " + "; ".join(report.missing_data) + "\n")
    blocks.append(texts.REPORT_FOOTER)

    return _pack(blocks)


def _pack(blocks: list[str]) -> list[str]:
    """Склеить блоки в сообщения, не разрывая блок пополам."""
    messages: list[str] = []
    current = ""
    for block in blocks:
        if len(current) + len(block) + 1 > TELEGRAM_LIMIT:
            if current:
                messages.append(current.rstrip())
            # Блок длиннее лимита сам по себе — режем по строкам.
            while len(block) > TELEGRAM_LIMIT:
                cut = block.rfind("\n", 0, TELEGRAM_LIMIT)
                cut = cut if cut > 0 else TELEGRAM_LIMIT
                messages.append(block[:cut].rstrip())
                block = block[cut:]
            current = block
        else:
            current += block + "\n"
    if current.strip():
        messages.append(current.rstrip())
    return messages
