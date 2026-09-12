"""Gemini: мультимодальный вход и строгий JSON на выходе.

Изображения и аудио модель берёт нативно, отдельный Whisper не нужен.

Общее правило для всех вызовов отсюда: модель получает JSON и возвращает JSON
по заданной схеме. Свободный текст не принимается — на нём номера РУ и цены
поплывут не сразу, а на десятом прогоне, и заметить это будет некому.
"""

from __future__ import annotations

import base64
import json
import logging
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from bot.config import get_settings
from bot.logging_setup import log_extra
from bot.services import pricing
from bot.services.http import ApiClient

logger = logging.getLogger(__name__)

API_BASE = "https://generativelanguage.googleapis.com/v1beta"

# Модель иногда оборачивает JSON в ```json ... ``` вопреки инструкции.
FENCE_RE = re.compile(r"^\s*```(?:json)?\s*(.*?)\s*```\s*$", re.DOTALL)


class GeminiError(RuntimeError):
    """Вызов не удался или ответ не разобрался."""


@dataclass(slots=True)
class Part:
    """Кусок мультимодального запроса: текст или файл."""

    text: str | None = None
    mime_type: str | None = None
    data: bytes | None = None

    def to_api(self) -> dict[str, Any]:
        if self.text is not None:
            return {"text": self.text}
        if self.data is None or self.mime_type is None:
            raise ValueError("часть без текста должна иметь mime_type и данные")
        return {
            "inline_data": {
                "mime_type": self.mime_type,
                "data": base64.b64encode(self.data).decode("ascii"),
            }
        }


@dataclass(slots=True)
class ProductRequest:
    """Результат разбора входа. Одна форма для текста, фото, голоса и файла."""

    product: str
    qty: str = "не указано"
    requirements: list[str] = field(default_factory=list)
    raw_input: str = ""
    confidence: float = 1.0
    transcript: str | None = None  # для голосовых: что именно услышали

    @property
    def recognised(self) -> bool:
        return bool(self.product.strip()) and self.product.strip().lower() not in (
            "не определено",
            "unknown",
            "null",
            "-",
        )


# --- Схемы структурированного вывода -------------------------------------

PRODUCT_SCHEMA: dict[str, Any] = {
    "type": "OBJECT",
    "properties": {
        "product": {"type": "STRING"},
        "qty": {"type": "STRING"},
        "requirements": {"type": "ARRAY", "items": {"type": "STRING"}},
        "confidence": {"type": "NUMBER"},
        "transcript": {"type": "STRING"},
    },
    "required": ["product", "qty", "requirements", "confidence"],
}

PRODUCT_INSTRUCTION = (
    "Ты помогаешь закупщику медицинских изделий. По входу определи, какое "
    "изделие нужно купить.\n"
    "Правила:\n"
    "- Название изделия пиши так, как его пишет производитель: тип, модель, "
    "производитель, если видны.\n"
    '- Не додумывай. Не разобрал — верни product="не определено" и '
    "confidence ниже 0.5. Пустое поле лучше правдоподобной выдумки: по этому "
    "названию дальше пойдёт проверка в государственном реестре.\n"
    "- qty — количество словами из запроса («10 штук», «партия»); не сказано — "
    '"не указано".\n'
    "- requirements — дополнительные требования закупщика (срок, сертификат, "
    "комплектация). Пусто, если их нет.\n"
    "- Для аудио обязательно заполни transcript — дословную расшифровку.\n"
    "- Отвечай по-русски."
)


class GeminiService:
    def __init__(self) -> None:
        settings = get_settings()
        self._settings = settings
        self._client = ApiClient(
            "gemini",
            base_url=API_BASE,
            headers={"Content-Type": "application/json"},
            timeout_read=120.0,  # мультимодальный запрос считается долго
        )

    async def aclose(self) -> None:
        await self._client.aclose()

    async def generate_json(
        self,
        *,
        parts: list[Part],
        system_instruction: str,
        schema: dict[str, Any] | None = None,
        model: str | None = None,
        request_id: int | None = None,
        operation: str = "generate",
        temperature: float = 0.1,
    ) -> dict[str, Any]:
        """Один вызов модели, ответ — разобранный JSON.

        Поднимает ``GeminiError``, если ключа нет, вызов не прошёл или ответ
        не удалось разобрать. Молча возвращать пустоту нельзя: наверху это
        станет «изделие не распознано», и владелец решит, что виновато фото.
        """
        settings = self._settings
        if not settings.gemini_enabled:
            raise GeminiError("GEMINI_API_KEY не задан")

        model_name = model or settings.llm_report_model
        body: dict[str, Any] = {
            "contents": [{"role": "user", "parts": [p.to_api() for p in parts]}],
            "systemInstruction": {"parts": [{"text": system_instruction}]},
            "generationConfig": {
                "temperature": temperature,
                "responseMimeType": "application/json",
            },
        }
        if schema is not None:
            body["generationConfig"]["responseSchema"] = schema

        result = await self._client.post(
            f"/models/{model_name}:generateContent",
            operation=f"{operation}:{model_name}",
            request_id=request_id,
            params={"key": settings.gemini_api_key},
            json=body,
        )
        if not result.ok:
            raise GeminiError(result.error or "вызов Gemini не удался")

        payload = result.json or {}
        usage = payload.get("usageMetadata", {})
        tokens_in = int(usage.get("promptTokenCount", 0) or 0)
        tokens_out = int(usage.get("candidatesTokenCount", 0) or 0)

        # Стоимость дописывается отдельной строкой: во время самого вызова
        # число токенов ещё неизвестно.
        if tokens_in or tokens_out:
            from bot.services.http import _record

            await _record(
                service="gemini",
                operation=f"{operation}:{model_name}:tokens",
                request_id=request_id,
                tokens_in=tokens_in,
                tokens_out=tokens_out,
                cost_usd=pricing.llm_cost(model_name, tokens_in, tokens_out),
                status="ok",
                duration_ms=0,
            )

        candidates = payload.get("candidates") or []
        if not candidates:
            reason = payload.get("promptFeedback", {}).get("blockReason")
            raise GeminiError(f"модель не вернула ответ (blockReason={reason})")

        text_parts = candidates[0].get("content", {}).get("parts", [])
        raw_text = "".join(part.get("text", "") for part in text_parts).strip()
        if not raw_text:
            raise GeminiError("модель вернула пустой текст")

        fenced = FENCE_RE.match(raw_text)
        if fenced:
            raw_text = fenced.group(1)

        try:
            parsed = json.loads(raw_text)
        except json.JSONDecodeError as exc:
            logger.error("Gemini вернул не JSON: %s", raw_text[:300], extra=log_extra(request_id))
            raise GeminiError(f"ответ не разобрался как JSON: {exc}") from exc

        if not isinstance(parsed, dict):
            raise GeminiError("ожидался объект JSON")
        return parsed

    # --- Разбор входа владельца ------------------------------------------

    async def parse_text(self, text: str, *, request_id: int | None = None) -> ProductRequest:
        parsed = await self.generate_json(
            parts=[Part(text=f"Запрос закупщика:\n{text}")],
            system_instruction=PRODUCT_INSTRUCTION,
            schema=PRODUCT_SCHEMA,
            request_id=request_id,
            operation="intake.text",
        )
        return _to_product_request(parsed, raw_input=text)

    async def parse_photo(
        self, image: bytes, mime_type: str = "image/jpeg", *, request_id: int | None = None
    ) -> ProductRequest:
        parsed = await self.generate_json(
            parts=[
                Part(text="На фото — медицинское изделие или его упаковка. Определи, что это."),
                Part(mime_type=mime_type, data=image),
            ],
            system_instruction=PRODUCT_INSTRUCTION,
            schema=PRODUCT_SCHEMA,
            request_id=request_id,
            operation="intake.photo",
        )
        return _to_product_request(parsed, raw_input="[фото]")

    async def parse_voice(
        self, audio: bytes, mime_type: str = "audio/ogg", *, request_id: int | None = None
    ) -> ProductRequest:
        parsed = await self.generate_json(
            parts=[
                Part(text="Это голосовое сообщение закупщика. Расшифруй и определи изделие."),
                Part(mime_type=mime_type, data=audio),
            ],
            system_instruction=PRODUCT_INSTRUCTION,
            schema=PRODUCT_SCHEMA,
            request_id=request_id,
            operation="intake.voice",
        )
        return _to_product_request(parsed, raw_input="[голосовое]")

    async def parse_document(
        self,
        content: bytes,
        mime_type: str,
        *,
        filename: str = "",
        request_id: int | None = None,
    ) -> ProductRequest:
        parsed = await self.generate_json(
            parts=[
                Part(text=f"Файл «{filename}» с описанием изделия. Определи, что нужно купить."),
                Part(mime_type=mime_type, data=content),
            ],
            system_instruction=PRODUCT_INSTRUCTION,
            schema=PRODUCT_SCHEMA,
            request_id=request_id,
            operation="intake.file",
        )
        return _to_product_request(parsed, raw_input=f"[файл {filename}]")

    async def transcribe(
        self, audio: bytes, mime_type: str = "audio/ogg", *, request_id: int | None = None
    ) -> str:
        """Только расшифровка, без разбора смысла (этап 6)."""
        parsed = await self.generate_json(
            parts=[
                Part(text="Расшифруй это голосовое сообщение дословно, по-русски."),
                Part(mime_type=mime_type, data=audio),
            ],
            system_instruction=(
                "Ты расшифровываешь речь. Верни ровно то, что сказано, без пересказа, "
                "без исправления оговорок и без своих комментариев."
            ),
            schema={
                "type": "OBJECT",
                "properties": {"transcript": {"type": "STRING"}},
                "required": ["transcript"],
            },
            request_id=request_id,
            operation="transcribe",
        )
        return str(parsed.get("transcript", "")).strip()

    async def run_prompt_file(
        self,
        prompt_name: str,
        payload: dict[str, Any],
        *,
        schema: dict[str, Any] | None = None,
        model: str | None = None,
        request_id: int | None = None,
        operation: str | None = None,
    ) -> dict[str, Any]:
        """Прогнать промпт из ``prompts/<name>.md`` над готовым JSON.

        Файл читается с диска при каждом вызове: владелец правит инструкцию и
        видит результат без перезапуска бота.
        """
        path = Path(self._settings.prompts_dir) / f"{prompt_name}.md"
        try:
            instruction = path.read_text(encoding="utf-8")
        except FileNotFoundError as exc:
            raise GeminiError(f"нет файла инструкции {path}") from exc

        return await self.generate_json(
            parts=[Part(text=json.dumps(payload, ensure_ascii=False, indent=2))],
            system_instruction=instruction,
            schema=schema,
            model=model,
            request_id=request_id,
            operation=operation or f"prompt.{prompt_name}",
        )


def _to_product_request(parsed: dict[str, Any], *, raw_input: str) -> ProductRequest:
    requirements = parsed.get("requirements") or []
    if not isinstance(requirements, list):
        requirements = [str(requirements)]
    transcript = parsed.get("transcript")
    return ProductRequest(
        product=str(parsed.get("product", "")).strip(),
        qty=str(parsed.get("qty", "не указано")).strip() or "не указано",
        requirements=[str(item) for item in requirements if str(item).strip()],
        raw_input=str(transcript).strip() if transcript else raw_input,
        confidence=float(parsed.get("confidence", 1.0) or 0.0),
        transcript=str(transcript).strip() if transcript else None,
    )


_service: GeminiService | None = None


def get_gemini_service() -> GeminiService:
    global _service
    if _service is None:
        _service = GeminiService()
    return _service


async def close_gemini_service() -> None:
    global _service
    if _service is not None:
        await _service.aclose()
    _service = None
