"""Поиск поставщиков через Perplexity.

Вызов прямой, не через Membrane: так требует ТЗ. Ответ приходит с источниками,
из них и берутся кандидаты — домены, а не «названия компаний», потому что
дедупликация в базе идёт по домену и ИНН.
"""

from __future__ import annotations

import json
import logging
import re
from dataclasses import dataclass, field

from bot.config import get_settings
from bot.logging_setup import log_extra
from bot.services import pricing
from bot.services.domains import normalise_domain
from bot.services.http import ApiClient

logger = logging.getLogger(__name__)

API_URL = "https://api.perplexity.ai/chat/completions"
DEFAULT_MODEL = "sonar"

# Домены, которые поиск возвращает постоянно и которые поставщиками не являются:
# агрегаторы, маркетплейсы, справочники, соцсети. Их отсекаем на входе.
NON_SUPPLIER_DOMAINS = frozenset(
    {
        "wikipedia.org",
        "ru.wikipedia.org",
        "youtube.com",
        "vk.com",
        "ok.ru",
        "t.me",
        "telegram.me",
        "facebook.com",
        "instagram.com",
        "twitter.com",
        "x.com",
        "avito.ru",
        "ozon.ru",
        "wildberries.ru",
        "market.yandex.ru",
        "aliexpress.ru",
        "rusprofile.ru",
        "list-org.com",
        "zachestnyibiznes.ru",
        "sbis.ru",
        "roszdravnadzor.gov.ru",
        "zakupki.gov.ru",
        "consultant.ru",
        "garant.ru",
    }
)

SEARCH_INSTRUCTION = (
    "Ты ищешь российских поставщиков медицинских изделий. Верни только "
    "компании, которые действительно продают указанное изделие: производители, "
    "официальные дистрибьюторы, специализированные поставщики медтехники.\n"
    "Не включай: маркетплейсы, агрегаторы объявлений, справочники юрлиц, "
    "новостные сайты, форумы.\n"
    "По каждой компании дай: название, сайт, e-mail и телефон, если они есть "
    "в источниках. Чего в источниках нет — оставь пустым, не придумывай.\n"
    "Ответ строго в JSON."
)

SUPPLIERS_SCHEMA_HINT = """Формат ответа (только JSON, без пояснений вокруг):
{
  "suppliers": [
    {"name": "...", "site": "https://...", "email": "...", "phone": "...", "note": "чем занимается"}
  ]
}
Пустые поля оставляй пустой строкой."""


@dataclass(slots=True)
class FoundSupplier:
    name: str
    site: str = ""
    email: str = ""
    phone: str = ""
    note: str = ""
    source_url: str = ""


@dataclass(slots=True)
class SearchOutcome:
    suppliers: list[FoundSupplier] = field(default_factory=list)
    citations: list[str] = field(default_factory=list)
    query: str = ""
    error: str | None = None

    @property
    def ok(self) -> bool:
        return self.error is None


EMAIL_RE = re.compile(r"[\w.+-]+@[\w-]+\.[\w.-]+")
FENCE_RE = re.compile(r"```(?:json)?\s*(.*?)\s*```", re.DOTALL)


def domain_of(url: str) -> str:
    """Ключ дедупликации выдачи — ровно тот же, что и у записи в базу."""
    return normalise_domain(url) or ""


def is_supplier_domain(url: str) -> bool:
    """Отсеивает агрегаторы и справочники — они не поставщики."""
    domain = domain_of(url)
    if not domain or "." not in domain:
        return False
    return not any(domain == bad or domain.endswith("." + bad) for bad in NON_SUPPLIER_DOMAINS)


class PerplexityService:
    def __init__(self) -> None:
        settings = get_settings()
        self._settings = settings
        self._client = ApiClient(
            "perplexity",
            headers={
                "Authorization": f"Bearer {settings.perplexity_api_key}",
                "Content-Type": "application/json",
            },
            timeout_read=90.0,
        )

    async def aclose(self) -> None:
        await self._client.aclose()

    async def find_suppliers(
        self,
        product: str,
        *,
        requirements: list[str] | None = None,
        request_id: int | None = None,
        model: str = DEFAULT_MODEL,
    ) -> SearchOutcome:
        """Найти поставщиков изделия."""
        settings = self._settings
        if not settings.search_enabled:
            return SearchOutcome(error="PERPLEXITY_API_KEY не задан")

        extras = f" Дополнительные требования: {'; '.join(requirements)}." if requirements else ""
        query = (
            f"Российские поставщики и дистрибьюторы медицинского изделия: {product}.{extras} "
            "Нужны названия компаний, их официальные сайты и контакты."
        )

        result = await self._client.post(
            API_URL,
            operation="search",
            request_id=request_id,
            cost_usd=pricing.flat_cost("perplexity"),
            json={
                "model": model,
                "messages": [
                    {
                        "role": "system",
                        "content": SEARCH_INSTRUCTION + "\n" + SUPPLIERS_SCHEMA_HINT,
                    },
                    {"role": "user", "content": query},
                ],
                "temperature": 0.1,
                "search_recency_filter": "year",
            },
        )
        if not result.ok:
            return SearchOutcome(query=query, error=result.error or "Perplexity недоступен")

        payload = result.json or {}
        choices = payload.get("choices") or []
        if not choices:
            return SearchOutcome(query=query, error="пустой ответ Perplexity")

        content = choices[0].get("message", {}).get("content", "")
        citations = [str(c) for c in (payload.get("citations") or [])]

        suppliers = _parse_suppliers(content, citations)
        logger.info(
            "Perplexity: по «%s» кандидатов %s, источников %s",
            product,
            len(suppliers),
            len(citations),
            extra=log_extra(request_id),
        )
        return SearchOutcome(suppliers=suppliers, citations=citations, query=query)


def _parse_suppliers(content: str, citations: list[str]) -> list[FoundSupplier]:
    """Разбор ответа модели плюс добор из списка источников.

    Модель иногда возвращает JSON, иногда прозу вокруг него, иногда только
    ссылки. Берём что удалось разобрать, а недостающие домены добираем из
    citations — они приходят структурированно и врать не умеют.
    """
    suppliers: list[FoundSupplier] = []
    seen: set[str] = set()

    text = content
    fenced = FENCE_RE.search(text)
    if fenced:
        text = fenced.group(1)

    try:
        parsed = json.loads(text)
        rows = parsed.get("suppliers", []) if isinstance(parsed, dict) else []
    except (json.JSONDecodeError, AttributeError):
        rows = []

    for row in rows:
        if not isinstance(row, dict):
            continue
        site = str(row.get("site") or "").strip()
        name = str(row.get("name") or "").strip()
        if not name:
            continue
        if site and not is_supplier_domain(site):
            continue
        key = domain_of(site) if site else name.lower()
        if key in seen:
            continue
        seen.add(key)
        email = str(row.get("email") or "").strip()
        suppliers.append(
            FoundSupplier(
                name=name,
                site=site,
                email=email if EMAIL_RE.fullmatch(email) else "",
                phone=str(row.get("phone") or "").strip(),
                note=str(row.get("note") or "").strip(),
                source_url=site,
            )
        )

    # Источники, которых модель не назвала явно, но которые похожи на сайты
    # поставщиков, тоже стоит проверить — их обойдёт Firecrawl.
    for url in citations:
        if not is_supplier_domain(url):
            continue
        key = domain_of(url)
        if key in seen:
            continue
        seen.add(key)
        suppliers.append(FoundSupplier(name=key, site=url, source_url=url))

    return suppliers


_service: PerplexityService | None = None


def get_perplexity_service() -> PerplexityService:
    global _service
    if _service is None:
        _service = PerplexityService()
    return _service


async def close_perplexity_service() -> None:
    global _service
    if _service is not None:
        await _service.aclose()
    _service = None
