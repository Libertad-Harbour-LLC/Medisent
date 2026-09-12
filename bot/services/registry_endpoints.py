"""Адаптер к реестрам Росздравнадзора: адреса и разбор ответов.

═══════════════════════════════════════════════════════════════════════════
ЕДИНСТВЕННЫЙ ФАЙЛ, КОТОРЫЙ НАДО ПРАВИТЬ, КОГДА ВЛАДЕЛЕЦ ДАСТ ТОЧНЫЕ ССЫЛКИ.
═══════════════════════════════════════════════════════════════════════════

``registry.py`` про формат ответов ничего не знает: он получает готовые
``RegistryRecord`` отсюда. Меняются здесь три вещи — путь запроса, имена полей
запроса и разбор ответа.

Как снять настоящие запросы к elk (порядок из ТЗ):

1. Открыть https://elk.roszdravnadzor.gov.ru/widget/ в обычном браузере.
2. F12 → вкладка Network → фильтр Fetch/XHR.
3. Ввести в виджет номер РУ или название изделия, нажать поиск.
4. Найти запрос к ``/public-gateway/med-product/api/v1/...`` — скопировать
   через «Copy as cURL» и сохранить ответ (Response → Copy).
5. Перенести путь в ``ELK_SEARCH_PATH``, имена полей — в ``build_elk_query``,
   разбор — в ``parse_elk_payload``. Сохранённый ответ положить в
   ``tests/fixtures/elk_found.json``, тест подхватит его автоматически.

Правило, которое нельзя нарушить при правке: если структура ответа не
разобралась, возвращается **пустой список и признак «непонятно»**, а вызов
наверху превращает это в статус ``unavailable``. Возвращать «ничего не нашли»
при неразобранном ответе запрещено: «не нашли» и «не смогли проверить» —
разные строки в отчёте.
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass, field
from html.parser import HTMLParser
from typing import Any

logger = logging.getLogger(__name__)

# --- Адреса --------------------------------------------------------------
# Базовые части приходят из .env (REGISTRY_ELK_BASE и т.д.), здесь — пути.

ELK_SEARCH_PATH = "/public-gateway/med-product/api/v1/registry/search"
ELK_CARD_URL_TEMPLATE = "https://elk.roszdravnadzor.gov.ru/widget/#/card/{record_id}"

# misearch — обычная страница с GET-параметрами. Пример из выдачи ТЗ:
#   ?q_mi_label_application=ФСР+2010/08183
MISEARCH_PARAM_RU = "q_mi_label_application"
MISEARCH_PARAM_NAME = "q_mi_name"

UNREGA_PARAM_NAME = "q_name"

# --- Маркеры «ничего не найдено» -----------------------------------------
# Нужны, чтобы отличить честное «в реестре такого нет» от «страница изменилась
# и мы её больше не понимаем». Без явного маркера второе НЕ выдаётся за первое.

NOT_FOUND_MARKERS = (
    "ничего не найдено",
    "не найдено записей",
    "по вашему запросу ничего",
    "результаты не найдены",
    "нет данных",
)

# Статусы действия РУ, как они пишутся в реестрах.
VALID_STATUS_MARKERS = ("действ",)                    # «действует», «действующее»
INVALID_STATUS_MARKERS = ("отмен", "аннулир", "прекращ", "приостанов", "недейств")


@dataclass(frozen=True, slots=True)
class RegistryRecord:
    """Одна запись реестра. Про поставщиков здесь нет ничего — только изделие."""

    registry: str                      # misearch | elk
    ru_number: str | None = None
    holder: str | None = None          # держатель РУ
    product_name: str | None = None
    valid: bool | None = None          # действует ли удостоверение
    status_text: str | None = None
    card_url: str | None = None
    raw: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True, slots=True)
class ParseOutcome:
    """Что удалось понять из ответа.

    ``understood=False`` означает «ответ пришёл, но мы его не разобрали» —
    наверху это станет ``unavailable``, а не ``not_found``.
    """

    records: list[RegistryRecord]
    understood: bool
    note: str | None = None


# --- elk (после 01.03.2025) ----------------------------------------------


def build_elk_query(*, name: str | None, ru_number: str | None) -> dict[str, Any]:
    """Тело запроса к gateway.

    ПРАВИТЬ ЗДЕСЬ: имена полей взять из снятого в DevTools запроса.
    """
    query: dict[str, Any] = {"page": 0, "size": 20}
    if ru_number:
        query["registrationNumber"] = ru_number
    if name:
        query["name"] = name
    return query


def _first(payload: dict[str, Any], *keys: str) -> Any:
    """Первое непустое значение из нескольких возможных имён поля."""
    for key in keys:
        value = payload.get(key)
        if value not in (None, "", [], {}):
            return value
    return None


_MISSING = object()


def _first_present(payload: dict[str, Any], *keys: str) -> Any:
    """Значение первого ПРИСУТСТВУЮЩЕГО ключа, даже если оно пустое.

    Для списка записей это принципиально: ``"content": []`` — валидный ответ
    «ничего не найдено», а не отсутствующее поле. Через ``_first`` пустой
    список выглядел бы как «структура не та», и честное «не найдено»
    превращалось бы в ``unavailable``.
    """
    for key in keys:
        if key in payload:
            return payload[key]
    return _MISSING


def _valid_from_status(status: str | None) -> bool | None:
    """Действует ли РУ. ``None`` — в ответе про это ничего не сказано."""
    if not status:
        return None
    lowered = status.lower()
    if any(marker in lowered for marker in INVALID_STATUS_MARKERS):
        return False
    if any(marker in lowered for marker in VALID_STATUS_MARKERS):
        return True
    return None


def parse_elk_payload(payload: Any) -> ParseOutcome:
    """Разбор JSON от gateway.

    ПРАВИТЬ ЗДЕСЬ: имена полей в ``_first(...)`` под реальный ответ.
    Пока перебираются наиболее вероятные варианты; если ни один не подошёл,
    запись не выдумывается — возвращается ``understood=False``.
    """
    if not isinstance(payload, dict):
        return ParseOutcome([], understood=False, note="ответ не объект JSON")

    rows = _first_present(payload, "content", "items", "data", "records", "result", "rows")
    if rows is _MISSING or rows is None:
        # Списка нет вообще — структура не та, что мы ждём.
        return ParseOutcome([], understood=False, note="в ответе нет списка записей")
    if isinstance(rows, dict):
        rows = _first(rows, "content", "items", "records") or []
    if not isinstance(rows, list):
        return ParseOutcome([], understood=False, note="список записей не является массивом")

    if not rows:
        # Список есть и он пустой — это честное «не нашли».
        total = _first(payload, "totalElements", "total", "totalCount")
        if total in (0, "0", None):
            return ParseOutcome([], understood=True, note="реестр вернул пустой список")
        return ParseOutcome([], understood=False, note=f"список пуст, но total={total}")

    records: list[RegistryRecord] = []
    for row in rows:
        if not isinstance(row, dict):
            continue
        status = _first(row, "status", "state", "statusName", "registrationStatus")
        status_text = str(status) if status is not None else None
        record_id = _first(row, "id", "uuid", "recordId")
        records.append(
            RegistryRecord(
                registry="elk",
                ru_number=_first(row, "registrationNumber", "regNumber", "number", "ruNumber"),
                holder=_first(row, "applicantName", "holder", "manufacturer", "organizationName"),
                product_name=_first(row, "name", "productName", "medProductName", "title"),
                valid=_valid_from_status(status_text),
                status_text=status_text,
                card_url=ELK_CARD_URL_TEMPLATE.format(record_id=record_id) if record_id else None,
                raw=row,
            )
        )

    if not records:
        return ParseOutcome([], understood=False, note="строки есть, но ни одна не разобралась")
    return ParseOutcome(records, understood=True)


# --- misearch (до 01.03.2025) --------------------------------------------


def build_misearch_params(*, name: str | None, ru_number: str | None) -> dict[str, str]:
    """GET-параметры страницы поиска."""
    params: dict[str, str] = {}
    if ru_number:
        params[MISEARCH_PARAM_RU] = ru_number
    if name:
        params[MISEARCH_PARAM_NAME] = name
    return params


class _TableRowParser(HTMLParser):
    """Вытаскивает строки таблиц из HTML средствами стандартной библиотеки.

    Отдельная зависимость вроде BeautifulSoup здесь не нужна: разметку всё
    равно придётся уточнять по живой странице, а лишний пакет в образе бота —
    это лишний пакет в образе бота.
    """

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.rows: list[list[str]] = []
        self._row: list[str] = []
        self._cell: list[str] = []
        self._in_cell = False

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if tag == "tr":
            self._row = []
        elif tag in ("td", "th"):
            self._in_cell = True
            self._cell = []

    def handle_endtag(self, tag: str) -> None:
        if tag in ("td", "th") and self._in_cell:
            self._row.append(" ".join("".join(self._cell).split()))
            self._in_cell = False
        elif tag == "tr":
            if any(cell for cell in self._row):
                self.rows.append(self._row)
            self._row = []

    def handle_data(self, data: str) -> None:
        if self._in_cell:
            self._cell.append(data)


RU_NUMBER_RE = re.compile(
    r"\b(?:ФСР|ФСЗ|РЗН)\s*[\dN№/\-\.]+(?:/\d+)?", re.IGNORECASE | re.UNICODE
)

# Организационно-правовые формы — по ним в строке таблицы опознаётся держатель РУ.
ORG_FORM_RE = re.compile(
    r"(?:\b(?:ООО|ОАО|ЗАО|ПАО|АО|НАО|ИП|НПО|НПП|ФГУП|ГУП|МУП)\b"
    r"|акционерное\s+общество"
    r"|общество\s+с\s+ограниченной"
    r"|индивидуальный\s+предприниматель"
    r"|\b(?:GmbH|Ltd|LLC|Inc|Corp|Corporation|S\.?p\.?A|B\.?V\.?|Co\.?)\b)",
    re.IGNORECASE | re.UNICODE,
)


def parse_misearch_html(html: str) -> ParseOutcome:
    """Разбор HTML-страницы старого реестра.

    ПРАВИТЬ ЗДЕСЬ: если у таблицы окажется другой порядок колонок, поправить
    ``_row_to_record``. Пока порядок определяется по содержимому, а не по
    номеру колонки — так разбор переживёт перестановку столбцов.
    """
    lowered = html.lower()
    if any(marker in lowered for marker in NOT_FOUND_MARKERS):
        return ParseOutcome([], understood=True, note="страница сообщила, что ничего не найдено")

    parser = _TableRowParser()
    try:
        parser.feed(html)
    except Exception as exc:  # noqa: BLE001
        return ParseOutcome([], understood=False, note=f"HTML не разобрался: {exc}")

    records = [rec for row in parser.rows if (rec := _row_to_record(row)) is not None]
    if records:
        return ParseOutcome(records, understood=True)

    # Таблиц нет и маркера «не найдено» нет: скорее всего разметка изменилась
    # либо страница отдала заглушку. Выдавать это за «не найдено» нельзя.
    return ParseOutcome(
        [], understood=False, note="ни таблицы с РУ, ни маркера «ничего не найдено»"
    )


def _row_to_record(cells: list[str]) -> RegistryRecord | None:
    """Строка таблицы → запись. Строки без номера РУ пропускаются как шапки.

    Держатель РУ определяется по признакам организационно-правовой формы, а не
    по длине ячейки: название изделия сплошь и рядом длиннее названия
    компании, и «самая длинная ячейка» уверенно выбирала не то поле.
    """
    joined = " | ".join(cells)
    match = RU_NUMBER_RE.search(joined)
    if not match:
        return None

    ru_number = match.group(0).strip()
    status_text = next(
        (
            cell
            for cell in cells
            if any(m in cell.lower() for m in VALID_STATUS_MARKERS + INVALID_STATUS_MARKERS)
        ),
        None,
    )
    candidates = [
        cell
        for cell in cells
        if cell and cell != status_text and not RU_NUMBER_RE.search(cell)
    ]
    if not candidates:
        return RegistryRecord(
            registry="misearch",
            ru_number=ru_number,
            valid=_valid_from_status(status_text),
            status_text=status_text,
            raw={"cells": cells},
        )

    holder = next((cell for cell in candidates if ORG_FORM_RE.search(cell)), None)
    if holder is None:
        # Признака формы нет — в этих таблицах держатель идёт последним
        # столбцом после номера РУ.
        holder = candidates[-1]
    product = next((cell for cell in candidates if cell != holder), None)

    return RegistryRecord(
        registry="misearch",
        ru_number=ru_number,
        holder=holder,
        product_name=product,
        valid=_valid_from_status(status_text),
        status_text=status_text,
        card_url=None,
        raw={"cells": cells},
    )


# --- unrega (информационные письма) --------------------------------------


def build_unrega_params(*, name: str) -> dict[str, str]:
    return {UNREGA_PARAM_NAME: name}


def parse_unrega_html(html: str) -> ParseOutcome:
    """Информационные письма об изъятиях и претензиях к качеству.

    Это негативный сигнал для базы критериев, а не проверка регистрации.
    """
    lowered = html.lower()
    if any(marker in lowered for marker in NOT_FOUND_MARKERS):
        return ParseOutcome([], understood=True, note="писем нет")

    parser = _TableRowParser()
    try:
        parser.feed(html)
    except Exception as exc:  # noqa: BLE001
        return ParseOutcome([], understood=False, note=f"HTML не разобрался: {exc}")

    records = [
        RegistryRecord(
            registry="unrega",
            product_name=" | ".join(row)[:500],
            raw={"cells": row},
        )
        for row in parser.rows
        if len(row) >= 2 and any(len(cell) > 10 for cell in row)
    ]
    if records:
        return ParseOutcome(records, understood=True)
    return ParseOutcome([], understood=False, note="таблица писем не разобралась")
