"""Потолок расходов на одну заявку.

Дневной потолок из ТЗ мягкий: бот предупреждает и продолжает. Этого мало —
предупреждение приходит фоновой проверкой раз в четверть часа, уже после того,
как деньги ушли. Одна заявка успевает сделать до десяти скрейпов, два
обращения к реестрам и несколько вызовов модели.

Здесь второй потолок, жёсткий и на заявку: превышен — платные вызовы по этой
заявке перестают уходить, конвейер доделывает работу на том, что успел
собрать, и говорит об этом владельцу. Заявка не обрывается на середине, но и
не съедает бюджет целиком.

Счётчик держится в памяти процесса и подсевается из базы при первом обращении
к заявке: после перезапуска потолок восстанавливается, а не обнуляется.
Растёт он в ``http._record`` — единственном месте учёта: каждый удачный
платный вызов добавляет свою цену до того, как уйдёт следующий.
"""

from __future__ import annotations

import logging
from decimal import Decimal

from bot.config import get_settings
from bot.logging_setup import log_extra

logger = logging.getLogger(__name__)

_spent: dict[int, Decimal] = {}
_warned: set[int] = set()


async def _seed(request_id: int) -> Decimal:
    """Подтянуть уже потраченное по заявке из базы."""
    try:
        from bot.db import repo
        from bot.db.session import session_scope

        async with session_scope() as session:
            return await repo.spent_on_request(session, request_id)
    except Exception:
        logger.exception("Не удалось прочитать расходы по заявке %s", request_id)
        return Decimal(0)


async def allow(request_id: int | None, cost: Decimal | None) -> bool:
    """Можно ли потратить ``cost`` по этой заявке.

    Бесплатные вызовы (реестры, Gmail) и вызовы вне заявки не ограничиваются:
    потолок про деньги, а не про количество запросов.
    """
    settings = get_settings()
    limit = Decimal(str(settings.max_cost_per_request_usd))
    if request_id is None or limit <= 0 or not cost:
        return True

    if request_id not in _spent:
        _spent[request_id] = await _seed(request_id)

    if _spent[request_id] + cost > limit:
        if request_id not in _warned:
            _warned.add(request_id)
            logger.warning(
                "Потолок на заявку исчерпан: потрачено $%s из $%s, вызов не отправлен",
                _spent[request_id],
                limit,
                extra=log_extra(request_id),
            )
        return False
    return True


async def allow_unpriced(request_id: int | None) -> bool:
    """Платный вызов, цена которого известна только по ответу (токены модели).

    Оценить его заранее нечем, поэтому решает уже потраченное: потолок
    достигнут — вызов не уходит. Иначе вызовы модели шли бы мимо потолка
    вовсе, а они — самая дорогая часть заявки.
    """
    settings = get_settings()
    limit = Decimal(str(settings.max_cost_per_request_usd))
    if request_id is None or limit <= 0:
        return True

    if request_id not in _spent:
        _spent[request_id] = await _seed(request_id)

    if _spent[request_id] >= limit:
        if request_id not in _warned:
            _warned.add(request_id)
            logger.warning(
                "Потолок на заявку исчерпан: потрачено $%s из $%s, вызов модели не отправлен",
                _spent[request_id],
                limit,
                extra=log_extra(request_id),
            )
        return False
    return True


def record(request_id: int | None, cost: Decimal | None) -> None:
    """Учесть потраченное. Вызывается после успешного платного вызова."""
    if request_id is None or not cost:
        return
    _spent[request_id] = _spent.get(request_id, Decimal(0)) + cost


def exceeded(request_id: int | None) -> bool:
    """Упиралась ли заявка в потолок — чтобы сказать об этом владельцу."""
    return request_id is not None and request_id in _warned


def spent(request_id: int) -> Decimal:
    return _spent.get(request_id, Decimal(0))


def forget(request_id: int) -> None:
    """Забыть заявку. Вызывается при закрытии, чтобы словарь не рос."""
    _spent.pop(request_id, None)
    _warned.discard(request_id)


def reset() -> None:
    """Только для тестов."""
    _spent.clear()
    _warned.clear()
