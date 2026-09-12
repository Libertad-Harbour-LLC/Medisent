"""Скрейп сайтов поставщиков через Firecrawl.

Только сайты поставщиков. К gateway реестра elk Firecrawl не применяется —
там свой JSON-API, и это отдельное жёсткое правило проекта.

Со страницы берутся четыре вещи: заявляет ли поставщик наличие позиции, цена,
e-mail, телефон. Всё, что пришло со страницы, — недоверенный текст: он проходит
через ``guard`` прежде чем попасть в модель.
"""

from __future__ import annotations

import asyncio
import logging
import re
from dataclasses import dataclass
from decimal import Decimal, InvalidOperation

from bot.config import get_settings
from bot.logging_setup import log_extra
from bot.services import guard, pricing
from bot.services.http import ApiClient

logger = logging.getLogger(__name__)

API_URL = "https://api.firecrawl.dev/v1/scrape"

EMAIL_RE = re.compile(r"[\w.+-]+@[\w-]+\.[\w.-]{2,}")
PHONE_RE = re.compile(r"(?:\+7|8)[\s\-(]*\d{3}[\s\-)]*\d{3}[\s\-]*\d{2}[\s\-]*\d{2}")
# Цена: число с необязательными разделителями тысяч и копейками, рядом рубли.
PRICE_RE = re.compile(
    r"(\d{1,3}(?:[\s ]\d{3})+|\d{4,9})(?:[.,](\d{1,2}))?\s*(?:руб|₽|r\.|rub)",
    re.IGNORECASE,
)
IN_STOCK_MARKERS = ("в наличии", "есть в наличии", "на складе", "готово к отгрузке", "in stock")
OUT_OF_STOCK_MARKERS = (
    "нет в наличии",
    "под заказ",
    "распродано",
    "снят с производства",
    "временно отсутствует",
    "out of stock",
)

# Почтовые ящики, которые встречаются на любом сайте и поставщика не идентифицируют.
GENERIC_EMAIL_PREFIXES = ("noreply", "no-reply", "postmaster", "abuse", "webmaster")


@dataclass(slots=True)
class ScrapeResult:
    url: str
    ok: bool = False
    claims_stock: bool | None = None
    price: Decimal | None = None
    email: str | None = None
    phone: str | None = None
    markdown: str = ""
    error: str | None = None
    injection_suspected: bool = False


def _extract_price(text: str) -> Decimal | None:
    """Первая правдоподобная цена в рублях.

    Берётся минимальная из найденных: на карточке товара крупные числа — это
    обычно «от 500 000 заказов» и телефоны, а не цена позиции.
    """
    prices: list[Decimal] = []
    for match in PRICE_RE.finditer(text):
        whole = re.sub(r"[\s ]", "", match.group(1))
        fraction = match.group(2) or "0"
        try:
            value = Decimal(f"{whole}.{fraction}")
        except InvalidOperation:
            continue
        # Отсекаем явный мусор: цена медизделия ниже 100 ₽ или выше 100 млн —
        # почти наверняка не цена.
        if Decimal(100) <= value <= Decimal(100_000_000):
            prices.append(value)
    return min(prices) if prices else None


def _extract_email(text: str) -> str | None:
    for match in EMAIL_RE.finditer(text):
        candidate = match.group(0).lower()
        local = candidate.split("@", 1)[0]
        if any(local.startswith(prefix) for prefix in GENERIC_EMAIL_PREFIXES):
            continue
        if candidate.endswith((".png", ".jpg", ".svg", ".webp")):
            continue
        return candidate
    return None


def _detect_stock(text: str, product: str) -> bool | None:
    """Заявляет ли сайт наличие. ``None`` — на странице об этом ничего нет.

    Это поле про сайт поставщика и только про него. С реестром Росздравнадзора
    оно не смешивается ни здесь, ни в отчёте.
    """
    lowered = text.lower()
    # Ищем маркер рядом с упоминанием изделия, иначе поймаем «в наличии» из
    # другого раздела каталога.
    keywords = [word for word in product.lower().split() if len(word) > 4][:3]
    window = lowered
    if keywords:
        positions = [lowered.find(word) for word in keywords if lowered.find(word) >= 0]
        if positions:
            start = max(0, min(positions) - 1500)
            window = lowered[start : min(positions) + 3000]

    has_in = any(marker in window for marker in IN_STOCK_MARKERS)
    has_out = any(marker in window for marker in OUT_OF_STOCK_MARKERS)
    if has_in and not has_out:
        return True
    if has_out and not has_in:
        return False
    if has_in and has_out:
        return None  # противоречие — честнее сказать «непонятно»
    return None


class FirecrawlService:
    def __init__(self) -> None:
        settings = get_settings()
        self._settings = settings
        self._client = ApiClient(
            "firecrawl",
            headers={
                "Authorization": f"Bearer {settings.firecrawl_api_key}",
                "Content-Type": "application/json",
            },
            timeout_read=120.0,
        )
        self._semaphore = asyncio.Semaphore(settings.scrape_concurrency)

    async def aclose(self) -> None:
        await self._client.aclose()

    async def scrape(
        self, url: str, product: str, *, request_id: int | None = None
    ) -> ScrapeResult:
        """Один сайт поставщика."""
        if not self._settings.scrape_enabled:
            return ScrapeResult(url=url, error="FIRECRAWL_API_KEY не задан")

        async with self._semaphore:
            result = await self._client.post(
                API_URL,
                operation="scrape",
                request_id=request_id,
                cost_usd=pricing.flat_cost("firecrawl"),
                json={
                    "url": url,
                    "formats": ["markdown"],
                    "onlyMainContent": True,
                    "timeout": 60000,
                },
            )

        if not result.ok:
            return ScrapeResult(url=url, error=result.error or "Firecrawl не ответил")

        payload = (result.json or {}).get("data") or {}
        markdown = str(payload.get("markdown") or "")
        if not markdown.strip():
            return ScrapeResult(url=url, error="страница пустая")

        # Всё, что пришло со страницы, — чужой текст. Проверяем до того, как
        # он попадёт в промпт отчёта.
        screening = await guard.screen_third_party_async(markdown, source=f"сайт {url}")

        return ScrapeResult(
            url=url,
            ok=True,
            claims_stock=_detect_stock(markdown, product),
            price=_extract_price(markdown),
            email=_extract_email(markdown),
            phone=(m.group(0) if (m := PHONE_RE.search(markdown)) else None),
            markdown=markdown,
            injection_suspected=screening.suspicious,
        )

    async def scrape_many(
        self, urls: list[str], product: str, *, request_id: int | None = None
    ) -> list[ScrapeResult]:
        """Обойти сайты параллельно, но не больше ``SCRAPE_CONCURRENCY`` сразу.

        Без ограничения десяток одновременных запросов упрётся в лимиты
        Firecrawl и вернёт 429 по половине списка.
        """
        if not urls:
            return []
        results = await asyncio.gather(
            *(self.scrape(url, product, request_id=request_id) for url in urls),
            return_exceptions=True,
        )
        out: list[ScrapeResult] = []
        for url, item in zip(urls, results, strict=True):
            if isinstance(item, BaseException):
                logger.warning("Скрейп %s упал: %s", url, item, extra=log_extra(request_id))
                out.append(ScrapeResult(url=url, error=str(item)))
            else:
                out.append(item)
        return out


_service: FirecrawlService | None = None


def get_firecrawl_service() -> FirecrawlService:
    global _service
    if _service is None:
        _service = FirecrawlService()
    return _service


async def close_firecrawl_service() -> None:
    global _service
    if _service is not None:
        await _service.aclose()
    _service = None
