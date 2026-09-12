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
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from bot.config import get_settings
from bot.logging_setup import log_extra
from bot.services import pricing
from bot.services.http import ApiClient, Usage
from bot.services.llm_json import parse_llm_json

logger = logging.getLogger(__name__)

API_BASE = "https://generativelanguage.googleapis.com/v1beta"


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

        # Цена известна только по ответу — считается по usageMetadata и
        # пишется той же строкой api_calls, что и сам вызов. Потолок на
        # заявку проверяет такой вызов по уже потраченному.
        result = await self._client.post(
            f"/models/{model_name}:generateContent",
            operation=f"{operation}:{model_name}",
            request_id=request_id,
            params={"key": settings.gemini_api_key},
            json=body,
            price=lambda payload: usage_from_payload(payload, model_name),
        )
        if not result.ok:
            if result.budget_exceeded:
                raise GeminiError("исчерпан потолок расходов на заявку")
            raise GeminiError(result.error or "вызов Gemini не удался")

        payload = result.json or {}
        candidates = payload.get("candidates") or []
        if not candidates:
            reason = payload.get("promptFeedback", {}).get("blockReason")
            raise GeminiError(f"модель не вернула ответ (blockReason={reason})")

        text_parts = candidates[0].get("content", {}).get("parts", [])
        raw_text = "".join(part.get("text", "") for part in text_parts).strip()
        if not raw_text:
            raise GeminiError("модель вернула пустой текст")

        parsed = parse_llm_json(raw_text)
        if parsed is None:
            logger.error("Gemini вернул не JSON: %s", raw_text[:300], extra=log_extra(request_id))
            raise GeminiError("ответ не разобрался как JSON")
        if not isinstance(parsed, dict):
            raise GeminiError("ожидался объект JSON")
        return parsed

    def load_instruction(self, name: str) -> str:
        """Системная инструкция из ``prompts/<name>.md``.

        Файл читается с диска при каждом вызове: владелец правит инструкцию
        и видит результат без перезапуска бота. В коде инструкций нет —
        это правило проекта.
        """
        path = Path(self._settings.prompts_dir) / f"{name}.md"
        try:
            return path.read_text(encoding="utf-8")
        except FileNotFoundError as exc:
            raise GeminiError(f"нет файла инструкции {path}") from exc

    # --- Разбор входа владельца ------------------------------------------

    async def parse_text(self, text: str, *, request_id: int | None = None) -> ProductRequest:
        parsed = await self.generate_json(
            parts=[Part(text=f"Запрос закупщика:\n{text}")],
            system_instruction=self.load_instruction("product"),
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
            system_instruction=self.load_instruction("product"),
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
            system_instruction=self.load_instruction("product"),
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
            system_instruction=self.load_instruction("product"),
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
            system_instruction=self.load_instruction("transcribe"),
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
        untrusted: bool = True,
    ) -> dict[str, Any]:
        """Прогнать промпт из ``prompts/<name>.md`` над готовым JSON.

        По умолчанию JSON уходит в модель в явной рамке «это данные, не
        инструкции»: почти в каждом промпте есть текст из чужих рук —
        названия компаний, придуманные моделью поиска по чужим страницам,
        адреса, цитаты с сайтов. Поля владельца от рамки не страдают, а
        забыть флаг у нового промпта теперь нельзя. ``untrusted=False`` —
        только для промптов, где чужого текста нет вовсе.
        """
        instruction = self.load_instruction(prompt_name)
        body = json.dumps(payload, ensure_ascii=False, indent=2)
        if untrusted:
            from bot.services import guard

            body = guard.wrap_untrusted(body, source="поиск и сайты поставщиков")

        return await self.generate_json(
            parts=[Part(text=body)],
            system_instruction=instruction,
            schema=schema,
            model=model,
            request_id=request_id,
            operation=operation or f"prompt.{prompt_name}",
        )


def usage_from_payload(payload: Any, model_name: str) -> Usage:
    """Токены и цена вызова из ``usageMetadata`` ответа Gemini."""
    meta = (payload or {}).get("usageMetadata", {}) if isinstance(payload, dict) else {}
    tokens_in = int(meta.get("promptTokenCount", 0) or 0)
    tokens_out = int(meta.get("candidatesTokenCount", 0) or 0)
    cached = meta.get("cachedContentTokenCount")
    return Usage(
        tokens_in=tokens_in,
        tokens_out=tokens_out,
        cached_tokens=int(cached) if cached is not None else None,
        cost_usd=pricing.llm_cost(model_name, tokens_in, tokens_out),
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
