"""Чат и генерация текста через Claude-эндпоинт провайдера.

Эндпоинт /claude/v1/messages повторяет формат Anthropic Messages API, но это
прокси: авторизация Bearer вместо x-api-key, и есть самодельный параметр
thinkingFlag, которого в настоящем API нет. Опираемся только на то, что описано
в спецификации провайдера.

Важно: stream по умолчанию true — если не передать false явно, вернётся SSE.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Any

from .kie import KieClient

log = logging.getLogger(__name__)

MESSAGES_PATH = "/claude/v1/messages"


@dataclass(slots=True)
class ChatReply:
    text: str
    input_tokens: int
    output_tokens: int
    credits: float
    stop_reason: str


class ClaudeService:
    def __init__(self, client: KieClient, model: str, max_tokens: int) -> None:
        self._client = client
        self._model = model
        self._max_tokens = max_tokens

    async def ask(
        self,
        messages: list[dict[str, Any]],
        *,
        system: str | None = None,
    ) -> ChatReply:
        payload: dict[str, Any] = {
            "model": self._model,
            "messages": messages,
            "stream": False,  # иначе придёт SSE: в спеке default true
            "max_tokens": self._max_tokens,
        }
        if system:
            # Прокси документирует только messages; системную роль кладём первой
            # реплики пользователя не трогая, чтобы не ломать формат.
            payload["system"] = system

        body = await self._client.post(MESSAGES_PATH, payload)
        usage = body.get("usage") or {}

        return ChatReply(
            text=_extract_text(body.get("content")),
            input_tokens=int(usage.get("input_tokens") or 0),
            output_tokens=int(usage.get("output_tokens") or 0),
            credits=float(body.get("credits_consumed") or 0.0),
            stop_reason=str(body.get("stop_reason") or ""),
        )


def _extract_text(content: Any) -> str:
    """Собирает текст из блоков ответа.

    Провайдер в примерах показывает только блоки tool_use, но формат Anthropic
    отдаёт текст как {"type": "text", "text": "..."}. Обрабатываем оба варианта
    и строку целиком — на случай, если прокси упростит ответ.
    """
    if isinstance(content, str):
        return content.strip()

    if not isinstance(content, list):
        return ""

    parts: list[str] = []
    for block in content:
        if isinstance(block, str):
            parts.append(block)
        elif isinstance(block, dict) and block.get("type") == "text":
            text = block.get("text")
            if isinstance(text, str):
                parts.append(text)

    return "\n".join(parts).strip()
