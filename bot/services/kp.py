"""Коммерческое предложение: извлечение цен из письма и сборка PDF.

Порядок из ТЗ, менять нельзя:

1. модель вытаскивает из письма позиции и цены в JSON;
2. **числа показываются владельцу на подтверждение в Telegram**;
3. только после подтверждения собирается PDF.

Цены в письмах приходят с оговорками — «без НДС», «от 10 штук», «при 100%
предоплате». Поэтому оговорки извлекаются отдельными полями, а не мнутся в
одно число: неверно вытащенная цена уйдёт клиенту под печатью владельца.

Печать и подпись ставятся только на финальную версию. Черновик — всегда
``--no-stamp``, и обойти это правило автоматизацией нельзя: флаг здесь
вычисляется из явного аргумента ``final``, значение по умолчанию — черновик.
"""

from __future__ import annotations

import asyncio
import datetime as dt
import json
import logging
import sys
import tempfile
from dataclasses import dataclass, field
from decimal import Decimal
from pathlib import Path
from typing import Any

from bot.config import get_settings
from bot.logging_setup import log_extra
from bot.services import guard
from bot.services.gemini import GeminiError, Part, get_gemini_service

logger = logging.getLogger(__name__)

# Порог «похоже на ошибку на порядок»: цена отличается от медианы по позициям
# больше чем в 20 раз. Скрипт сборки цены не проверяет — это делаем мы.
ORDER_OF_MAGNITUDE_FACTOR = Decimal(20)

VALID_UNTIL_DAYS = 14

EXTRACTION_SCHEMA: dict[str, Any] = {
    "type": "OBJECT",
    "properties": {
        "items": {
            "type": "ARRAY",
            "items": {
                "type": "OBJECT",
                "properties": {
                    "name": {"type": "STRING"},
                    "note": {"type": "STRING"},
                    "qty": {"type": "NUMBER"},
                    "unit": {"type": "STRING"},
                    "price": {"type": "NUMBER"},
                    "vat_included": {"type": "BOOLEAN", "nullable": True},
                    "min_qty": {"type": "NUMBER", "nullable": True},
                    "prepayment_pct": {"type": "NUMBER", "nullable": True},
                    "caveat": {"type": "STRING"},
                },
                "required": ["name", "qty", "price"],
            },
        },
        "currency": {"type": "STRING"},
        "lead_time": {"type": "STRING"},
        "payment_terms": {"type": "STRING"},
        "valid_until": {"type": "STRING"},
        "notes": {"type": "ARRAY", "items": {"type": "STRING"}},
    },
    "required": ["items", "currency"],
}

EXTRACTION_INSTRUCTION = (
    "Ты разбираешь письмо поставщика медицинских изделий и вытаскиваешь позиции "
    "и цены.\n\n"
    "ГЛАВНОЕ ПРАВИЛО: цены в письмах идут с оговорками, и оговорку нельзя "
    "терять в числе.\n"
    '- «12 500 без НДС» → price=12500, vat_included=false, caveat="без НДС"\n'
    '- «от 10 шт по 11 000» → price=11000, min_qty=10, caveat="цена от 10 штук"\n'
    "- «12 500 при 100% предоплате» → price=12500, prepayment_pct=100\n"
    "- Оговорку, которую не удалось разложить по полям, клади целиком в caveat.\n\n"
    "Чего в письме нет — не придумывай. Нет количества — ставь qty=1 и пиши это "
    "в caveat. Нет цены по позиции — позицию не включай вовсе.\n"
    "Диапазон «от 10 до 12 тысяч» — бери нижнюю границу и напиши это в caveat.\n"
    "currency: RUB, USD или EUR. Не указана явно и суммы в рублях — RUB.\n"
    "Отвечай только JSON."
)


@dataclass(slots=True)
class ExtractedItem:
    name: str
    qty: Decimal
    price: Decimal
    unit: str = "шт."
    note: str = ""
    vat_included: bool | None = None
    min_qty: Decimal | None = None
    prepayment_pct: Decimal | None = None
    caveat: str = ""

    @property
    def total(self) -> Decimal:
        return (self.qty * self.price).quantize(Decimal("0.01"))

    @property
    def caveats(self) -> list[str]:
        """Все оговорки одной строкой — их владелец и должен увидеть."""
        out: list[str] = []
        if self.vat_included is False:
            out.append("без НДС")
        elif self.vat_included is True:
            out.append("с НДС")
        if self.min_qty:
            out.append(f"от {self.min_qty:g} шт.")
        if self.prepayment_pct:
            out.append(f"предоплата {self.prepayment_pct:g}%")
        if self.caveat:
            out.append(self.caveat)
        return out


@dataclass(slots=True)
class Extraction:
    items: list[ExtractedItem] = field(default_factory=list)
    currency: str = "RUB"
    lead_time: str = ""
    payment_terms: str = ""
    valid_until: str = ""
    notes: list[str] = field(default_factory=list)
    failed: bool = False
    error: str = ""

    @property
    def total(self) -> Decimal:
        return sum((item.total for item in self.items), Decimal(0))

    def suspicious_items(self) -> list[ExtractedItem]:
        """Позиции, чья цена отличается от медианы на порядок и больше.

        ТЗ: цена, похожая на ошибку на порядок, — повод спросить, а не
        промолчать. Скрипт сборки цены не проверяет, только считает суммы.
        """
        prices = sorted(item.price for item in self.items if item.price > 0)
        if len(prices) < 3:
            return []
        median = prices[len(prices) // 2]
        if median <= 0:
            return []
        return [
            item
            for item in self.items
            if item.price > median * ORDER_OF_MAGNITUDE_FACTOR
            or item.price * ORDER_OF_MAGNITUDE_FACTOR < median
        ]


def _decimal(value: Any, default: Decimal | None = None) -> Decimal | None:
    if value is None or value == "":
        return default
    try:
        return Decimal(str(value))
    except (ArithmeticError, ValueError):
        return default


async def extract_from_letter(
    letter_text: str,
    *,
    attachments_text: str = "",
    request_id: int | None = None,
) -> Extraction:
    """Разобрать письмо поставщика.

    Текст письма — чужой, поэтому идёт в модель через ``guard``: в письме от
    незнакомого адресата может лежать и цена, и попытка перехвата инструкций.
    """
    combined = letter_text
    if attachments_text:
        combined += f"\n\n--- из вложений ---\n{attachments_text}"

    safe = await guard.sanitise_for_model(combined, source="письмо поставщика")

    try:
        parsed = await get_gemini_service().generate_json(
            parts=[Part(text=safe)],
            system_instruction=EXTRACTION_INSTRUCTION,
            schema=EXTRACTION_SCHEMA,
            model=get_settings().llm_email_model,
            request_id=request_id,
            operation="kp.extract",
        )
    except GeminiError as exc:
        logger.error("Разбор письма не удался: %s", exc, extra=log_extra(request_id))
        return Extraction(failed=True, error=str(exc))

    items: list[ExtractedItem] = []
    for row in parsed.get("items") or []:
        if not isinstance(row, dict):
            continue
        name = str(row.get("name") or "").strip()
        price = _decimal(row.get("price"))
        qty = _decimal(row.get("qty"), Decimal(1)) or Decimal(1)
        if not name or price is None or price <= 0:
            continue
        items.append(
            ExtractedItem(
                name=name,
                qty=qty,
                price=price,
                unit=str(row.get("unit") or "шт."),
                note=str(row.get("note") or ""),
                vat_included=row.get("vat_included"),
                min_qty=_decimal(row.get("min_qty")),
                prepayment_pct=_decimal(row.get("prepayment_pct")),
                caveat=str(row.get("caveat") or ""),
            )
        )

    notes = parsed.get("notes") or []
    return Extraction(
        items=items,
        currency=str(parsed.get("currency") or "RUB").upper(),
        lead_time=str(parsed.get("lead_time") or ""),
        payment_terms=str(parsed.get("payment_terms") or ""),
        valid_until=str(parsed.get("valid_until") or ""),
        notes=[str(n) for n in notes if str(n).strip()] if isinstance(notes, list) else [],
    )


def read_pdf_attachment(content: bytes) -> str:
    """Текст и таблицы из PDF-прайса поставщика.

    Прайсы приходят вложением, и цена в них обычно в таблице, а не в тексте
    письма. Таблицы вытаскиваются отдельно: без них строки прайса склеиваются
    в кашу и модель читает их неверно.
    """
    try:
        import pdfplumber
    except ImportError:
        logger.warning("pdfplumber не установлен — вложение PDF пропущено")
        return ""

    chunks: list[str] = []
    with tempfile.NamedTemporaryFile(suffix=".pdf") as handle:
        handle.write(content)
        handle.flush()
        try:
            with pdfplumber.open(handle.name) as pdf:
                for number, page in enumerate(pdf.pages, start=1):
                    text = page.extract_text() or ""
                    if text.strip():
                        chunks.append(f"[страница {number}]\n{text}")
                    for table in page.extract_tables() or []:
                        rows = [
                            " | ".join(cell or "" for cell in row)
                            for row in table
                            if any(cell for cell in row)
                        ]
                        if rows:
                            chunks.append(f"[таблица, страница {number}]\n" + "\n".join(rows))
        except Exception as exc:
            logger.warning("PDF не разобрался: %s", exc)
            return ""
    return "\n\n".join(chunks)


def build_kp_json(
    extraction: Extraction,
    *,
    number: str,
    client_name: str,
    intro: str = "",
) -> tuple[dict[str, Any], str | None]:
    """Собрать входной JSON для ``build_kp.py``.

    Возвращает ``(данные, предупреждение)``. Предупреждение непустое, если
    ``valid_until`` пришлось проставить самим: правило скилла — сообщить об
    этом владельцу, а не проставить молча.
    """
    today = dt.date.today()
    warning: str | None = None
    valid_until = extraction.valid_until.strip()
    if not valid_until:
        default = today + dt.timedelta(days=VALID_UNTIL_DAYS)
        valid_until = default.strftime("%d.%m.%Y")
        warning = valid_until

    terms: list[str] = []
    if extraction.lead_time:
        terms.append(f"Срок поставки — {extraction.lead_time}.")
    if extraction.payment_terms:
        terms.append(f"Порядок оплаты — {extraction.payment_terms}.")
    terms.extend(extraction.notes)

    payload: dict[str, Any] = {
        "number": number,
        "date": today.strftime("%d.%m.%Y"),
        "valid_until": valid_until,
        "title": "Коммерческое предложение",
        "client": {"name": client_name},
        "currency": extraction.currency,
        "items": [
            {
                "name": item.name,
                "note": "; ".join(filter(None, [item.note, *item.caveats])),
                "qty": float(item.qty),
                "unit": item.unit,
                "price": float(item.price),
            }
            for item in extraction.items
        ],
        "terms": terms,
    }
    if intro:
        payload["intro"] = intro
    return payload, warning


def check_assets() -> list[str]:
    """Проверить логотип, печать и подпись. Возвращает список отсутствующих."""
    settings = get_settings()
    missing: list[str] = []
    for name in ("logo.png", "stamp.png", "signature.png"):
        if not (Path(settings.kp_builder_dir) / "assets" / name).exists():
            missing.append(name)
    return missing


async def build_pdf(
    data: dict[str, Any],
    *,
    out_path: Path,
    final: bool = False,
    request_id: int | None = None,
) -> tuple[bool, str]:
    """Собрать PDF скриптом скилла. Возвращает ``(успех, вывод скрипта)``.

    ``final=False`` — черновик, идёт с ``--no-stamp``. Значение по умолчанию
    именно такое: печать и подпись должны требовать явного решения, а не
    получаться сами собой.
    """
    settings = get_settings()
    kp_dir = Path(settings.kp_builder_dir)
    script = kp_dir / "scripts" / "build_kp.py"
    if not script.exists():
        return False, f"нет скрипта сборки {script}"

    out_path.parent.mkdir(parents=True, exist_ok=True)

    with tempfile.NamedTemporaryFile("w", suffix=".json", delete=False, encoding="utf-8") as handle:
        json.dump(data, handle, ensure_ascii=False, indent=2)
        data_path = Path(handle.name)

    args = [sys.executable, str(script), "--data", str(data_path), "--out", str(out_path)]
    if not final:
        args.append("--no-stamp")

    logger.info(
        "Сборка КП: %s",
        "финальная с печатью" if final else "черновик без печати",
        extra=log_extra(request_id),
    )
    try:
        process = await asyncio.create_subprocess_exec(
            *args,
            cwd=str(kp_dir),
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.STDOUT,
        )
        stdout, _ = await asyncio.wait_for(process.communicate(), timeout=180)
        output = stdout.decode("utf-8", errors="replace")
        success = process.returncode == 0 and await asyncio.to_thread(out_path.exists)
        if not success:
            logger.error("Сборка КП не удалась: %s", output[-1000:], extra=log_extra(request_id))
        return success, output
    except TimeoutError:
        return False, "сборка КП не уложилась в 180 секунд"
    finally:
        await asyncio.to_thread(data_path.unlink, True)


# --- Сериализация для таблицы одобрений ----------------------------------
#
# В одобрении хранится ровно то, что показали владельцу. Собирается КП потом
# из этой записи, а не из свежего разбора письма: между показом и нажатием
# «Да» модель могла бы разобрать письмо иначе.


def extraction_to_payload(extraction: Extraction) -> dict[str, Any]:
    """Разбор цен → JSON для колонки ``approvals.payload``."""
    return {
        "currency": extraction.currency,
        "lead_time": extraction.lead_time,
        "payment_terms": extraction.payment_terms,
        "valid_until": extraction.valid_until,
        "notes": list(extraction.notes),
        "items": [
            {
                "name": item.name,
                "qty": str(item.qty),
                "price": str(item.price),
                "unit": item.unit,
                "note": item.note,
                "vat_included": item.vat_included,
                "min_qty": str(item.min_qty) if item.min_qty is not None else None,
                "prepayment_pct": (
                    str(item.prepayment_pct) if item.prepayment_pct is not None else None
                ),
                "caveat": item.caveat,
            }
            for item in extraction.items
        ],
    }


def extraction_from_payload(payload: dict[str, Any]) -> Extraction:
    """Обратно из одобрения. Decimal восстанавливается из строк, а не из float:
    цена в документе с подписью не должна поехать на копейку."""
    items = [
        ExtractedItem(
            name=str(row.get("name", "")),
            qty=Decimal(str(row.get("qty", "1"))),
            price=Decimal(str(row.get("price", "0"))),
            unit=str(row.get("unit", "шт.")),
            note=str(row.get("note", "")),
            vat_included=row.get("vat_included"),
            min_qty=Decimal(str(row["min_qty"])) if row.get("min_qty") else None,
            prepayment_pct=(
                Decimal(str(row["prepayment_pct"])) if row.get("prepayment_pct") else None
            ),
            caveat=str(row.get("caveat", "")),
        )
        for row in payload.get("items", [])
    ]
    return Extraction(
        items=items,
        currency=str(payload.get("currency", "RUB")),
        lead_time=str(payload.get("lead_time", "")),
        payment_terms=str(payload.get("payment_terms", "")),
        valid_until=str(payload.get("valid_until", "")),
        notes=[str(n) for n in payload.get("notes", [])],
    )
