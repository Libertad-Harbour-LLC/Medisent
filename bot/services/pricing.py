"""Оценка стоимости вызовов внешних сервисов.

Цифры — прейскуранты на момент написания, они устаревают. Смысл таблицы не в
копеечной точности, а в том, чтобы владелец видел порядок трат и срабатывал
мягкий потолок ``DAILY_API_BUDGET_USD``. Реальный счёт всегда в личном
кабинете сервиса.

Проверять актуальность:
* Gemini      — https://ai.google.dev/gemini-api/docs/pricing
* Perplexity  — https://docs.perplexity.ai/guides/pricing
* Firecrawl   — https://www.firecrawl.dev/pricing
"""

from __future__ import annotations

from decimal import Decimal

# Цена за миллион токенов, USD.
LLM_PRICES: dict[str, tuple[Decimal, Decimal]] = {
    # модель: (вход, выход)
    "gemini-flash-latest": (Decimal("0.30"), Decimal("2.50")),
    "gemini-flash-lite-latest": (Decimal("0.10"), Decimal("0.40")),
    "gemini-pro-latest": (Decimal("1.25"), Decimal("10.00")),
}
LLM_PRICE_FALLBACK = (Decimal("0.30"), Decimal("2.50"))

# Фиксированная цена за вызов, USD.
FLAT_PRICES: dict[str, Decimal] = {
    "perplexity": Decimal("0.006"),   # sonar, запрос среднего размера
    "firecrawl": Decimal("0.001"),    # один scrape
    "gmail": Decimal("0"),            # бесплатно в пределах квоты
    "registry": Decimal("0"),         # государственные реестры
    "browseract": Decimal("0.02"),    # если владелец включит поиск контактов
}


def llm_cost(model: str, tokens_in: int, tokens_out: int) -> Decimal:
    """Стоимость одного вызова модели по числу токенов."""
    price_in, price_out = LLM_PRICES.get(model, LLM_PRICE_FALLBACK)
    million = Decimal(1_000_000)
    return (
        price_in * Decimal(tokens_in) / million + price_out * Decimal(tokens_out) / million
    ).quantize(Decimal("0.000001"))


def flat_cost(service: str, calls: int = 1) -> Decimal:
    """Стоимость сервиса с фиксированным прайсом за вызов."""
    return (FLAT_PRICES.get(service, Decimal("0")) * Decimal(calls)).quantize(Decimal("0.000001"))
