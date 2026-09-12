"""База критериев выбора: извлечение из голосового и накопление.

Смысл этапа: после каждого выбора система становится чуть умнее. Владелец
объясняет, почему взял этого поставщика, — из объяснения вытаскивается
правило, применимое к следующим заявкам, и подмешивается в промпт отчёта.

Склейка «по смыслу» живёт в ``repo.upsert_criterion``: сначала точное
совпадение, потом триграммы. Здесь — только вызов модели и запись событий.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession

from bot.db import repo
from bot.logging_setup import log_extra
from bot.services.gemini import GeminiError, get_gemini_service

logger = logging.getLogger(__name__)

CRITERIA_SCHEMA: dict[str, Any] = {
    "type": "OBJECT",
    "properties": {
        "chosen_supplier_id": {"type": "INTEGER", "nullable": True},
        "criteria": {
            "type": "ARRAY",
            "items": {
                "type": "OBJECT",
                "properties": {
                    "text": {"type": "STRING"},
                    "direction": {"type": "STRING"},
                    "weight": {"type": "NUMBER"},
                    "supplier_id": {"type": "INTEGER", "nullable": True},
                    "same_as": {"type": "INTEGER", "nullable": True},
                },
                "required": ["text", "direction", "weight"],
            },
        },
        "wants_more_info_about": {"type": "INTEGER", "nullable": True},
    },
    "required": ["criteria"],
}


@dataclass(slots=True)
class ExtractedCriterion:
    text: str
    direction: str
    weight: float
    supplier_id: int | None = None
    same_as: int | None = None


@dataclass(slots=True)
class SelectionOutcome:
    """Что владелец сказал голосовым."""

    chosen_supplier_id: int | None = None
    wants_more_info_about: int | None = None
    criteria: list[ExtractedCriterion] = field(default_factory=list)
    transcript: str = ""
    failed: bool = False

    @property
    def is_choice(self) -> bool:
        return self.chosen_supplier_id is not None


async def extract(
    transcript: str,
    *,
    candidates: list[dict[str, Any]],
    known_criteria: list[dict[str, Any]],
    request_id: int | None = None,
) -> SelectionOutcome:
    """Прогнать расшифровку через ``prompts/criteria.md``."""
    payload = {
        "transcript": transcript,
        "candidates": candidates,
        "known_criteria": known_criteria,
    }
    try:
        parsed = await get_gemini_service().run_prompt_file(
            "criteria",
            payload,
            schema=CRITERIA_SCHEMA,
            request_id=request_id,
            operation="criteria.extract",
            # Названия кандидатов придумала модель поиска по чужим страницам.
            untrusted=True,
        )
    except GeminiError as exc:
        logger.error("Извлечение критериев не удалось: %s", exc, extra=log_extra(request_id))
        return SelectionOutcome(transcript=transcript, failed=True)

    # Любой id от модели сверяется со списком, который ей дали. Модель может
    # вернуть номер из отчёта вместо id, чужого поставщика или того, кого
    # чёрный список уже отсёк — ни один из них не должен стать адресатом
    # письма или попасть в колонку с внешним ключом.
    allowed = {c["id"] for c in candidates if isinstance(c.get("id"), int)}

    def _known(value: Any, field_name: str) -> int | None:
        number = _as_int(value)
        if number is None or number in allowed:
            return number
        logger.warning(
            "Модель вернула %s=%r, которого нет среди кандидатов %s",
            field_name,
            value,
            sorted(allowed),
            extra=log_extra(request_id),
        )
        return None

    rows = parsed.get("criteria") or []
    criteria: list[ExtractedCriterion] = []
    for row in rows:
        if not isinstance(row, dict):
            continue
        text_value = str(row.get("text") or "").strip()
        if not text_value:
            continue
        direction = str(row.get("direction") or "plus").lower()
        if direction not in ("plus", "minus"):
            direction = "plus"
        try:
            weight = float(row.get("weight") or 1.0)
        except (TypeError, ValueError):
            weight = 1.0
        criteria.append(
            ExtractedCriterion(
                text=text_value,
                direction=direction,
                # Вес держим в разумных границах: модель иногда выдаёт 10.
                weight=min(max(weight, 0.5), 2.0),
                supplier_id=_known(row.get("supplier_id"), "supplier_id"),
                same_as=_as_int(row.get("same_as")),
            )
        )

    return SelectionOutcome(
        chosen_supplier_id=_known(parsed.get("chosen_supplier_id"), "chosen_supplier_id"),
        wants_more_info_about=_known(parsed.get("wants_more_info_about"), "wants_more_info_about"),
        criteria=criteria,
        transcript=transcript,
    )


def _as_int(value: Any) -> int | None:
    try:
        return int(value) if value is not None else None
    except (TypeError, ValueError):
        return None


async def persist(
    session: AsyncSession,
    outcome: SelectionOutcome,
    *,
    request_id: int | None,
) -> tuple[int, int]:
    """Записать критерии и события. Возвращает ``(всего, из них новых)``.

    Транскрипт сохраняется с каждым событием: через полгода странный критерий
    надо будет чем-то объяснить, и «модель так решила» объяснением не будет.
    """
    total = 0
    created = 0
    events: list[tuple[int, int | None]] = []
    for item in outcome.criteria:
        try:
            criterion_id, is_new = await repo.upsert_criterion(
                session,
                text_value=item.text,
                direction=item.direction,
                weight=item.weight,
                same_as=item.same_as,
            )
        except ValueError:
            continue
        events.append((criterion_id, item.supplier_id))
        total += 1
        created += int(is_new)
    # События — одним пакетом, а не по одному на критерий.
    repo.record_criterion_events(
        session, events, request_id=request_id, transcript=outcome.transcript
    )

    logger.info(
        "Критериев записано: %s, из них новых %s", total, created, extra=log_extra(request_id)
    )
    return total, created


async def for_prompt(session: AsyncSession, limit: int = 20) -> list[dict[str, Any]]:
    """Накопленные критерии в том виде, в каком их ждёт ``prompts/report.md``."""
    rows = await repo.list_criteria(session, limit=limit)
    return [
        {
            "text": row.text,
            "direction": row.direction or "plus",
            "weight": round(float(row.weight), 2),
            "times_seen": int(row.times_seen),
        }
        for row in rows
    ]
